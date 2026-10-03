from collections import defaultdict, deque
from copy import deepcopy
import os
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
from urllib.parse import urlsplit


SCRIPT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
VISUAL = (".css", ".scss", ".sass", ".glsl", ".vert", ".frag")
EXCLUDED = {".git", "node_modules", "test", "tests", "__tests__", "__fixtures__",
            "dist", "build", "out", "coverage", "docs", "examples", ".yarn", ".github"}
MAX_LINES = 480
NONCODE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*.*?\*/', re.S)
IMPORT = re.compile(
    r"\b(?:import|export)\s+(?:(?:[^;'\"]*?)\s+from\s*)?['\"]([^'\"]+)['\"]"
    r"|\b(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|@(?:import|use|forward)\s+['\"]([^'\"]+)['\"]"
    r"|#include\s+[\"<]([^\">]+)[\">]", re.M,
)
DECLARATION = re.compile(r"\b(?:const|let|var|function|class)\s+([$A-Za-z_][$\w]*)")


class SourceTools:


    def __init__(self, repo, structure, *, evidence=False):
        self.repo = Path(repo).resolve()
        self.index = _index(self.repo, structure)
        self.evidence = evidence
        self.editable_files = set(self.index)
        self.base_commit = None
        self.omitted = []
        self.tracked_paths = []
        if evidence:
            self.base_commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
            entries = subprocess.check_output(["git", "-C", str(self.repo), "ls-tree", "-rz", "HEAD"]).decode().split("\0")
            indexed = {}
            for entry in filter(None, entries):
                metadata, name = entry.split("\t", 1)
                mode, kind, blob = metadata.split()
                self.tracked_paths.append(name)
                path = self.repo / name
                parts = set(PurePosixPath(name).parts)
                if mode not in {"100644", "100755"} or kind != "blob" or parts & {".git", "node_modules", "dist", "build", "out", "coverage", ".yarn", ".cache"}:
                    continue
                if path.suffix not in set(SCRIPT + VISUAL) | {".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".html", ".sh", ".lock"}:
                    continue
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.repo):
                    continue
                if any((self.repo / p).is_symlink() for p in PurePosixPath(name).parents):
                    continue
                if path.stat().st_size > 2_000_000:
                    self.omitted.append({"file": name, "reason": "file_size_limit"})
                    continue
                data = subprocess.check_output(["git", "-C", str(self.repo), "cat-file", "blob", blob])
                text = data.decode("utf-8", errors="replace")
                indexed[name] = {**self.index.get(name, {}), "text": text, "lines": text.splitlines(),
                                 "masked": _masked(text), "sha256": hashlib.sha256(data).hexdigest()}
            self.index = indexed
        self.read_files = []
        files = set(self.index)
        package = self.repo / "package.json"
        if package.is_file() and not package.is_symlink() and (not evidence or "package.json" in self.index):
            files.add("package.json")
        directories = {parent.as_posix() for name in files for parent in PurePosixPath(name).parents
                       if parent.as_posix() != "."}
        self.file_count = len(files)
        self.paths = ["", *sorted(files), *sorted(directories)]
        self.ids = {path: identifier for identifier, path in enumerate(self.paths)}

    def manifest(self):

        return {"base_commit": self.base_commit, "all_base_files": self.tracked_paths,
                "readable_files": {name: {"sha256": entry.get("sha256"), "editable": name in self.editable_files}
                                   for name, entry in self.index.items()}, "omitted": self.omitted}

    def catalog(self):

        groups = {path: {"id": self.ids[path], "path": path, "files": []}
                  for path in ["", *self.paths[self.file_count + 1:]]}
        for identifier, path in enumerate(self.paths[1:self.file_count + 1], 1):
            parent, _, name = path.rpartition("/")
            if not self.evidence or identifier <= 200:
                groups[parent]["files"].append([identifier, name])
        return {"protocol": "source_ids_v1", "file_count": self.file_count,
                "max_id": len(self.paths) - 1, "directories": list(groups.values()),
                **({"evidence_policy": "base", "base_commit": self.base_commit,
                    "file_listing_truncated": self.file_count > 200, "listing_page_size": 200,
                    "omitted": self.omitted, "additional_tools": ["list_sources", "project_info", "run_tests"]}
                   if self.evidence else {})}

    def execute(self, tool, output):

        identifier = tool["source_id"]
        if type(identifier) is not int or not 0 <= identifier < len(self.paths):
            return {"ok": False, "error": "unknown_source_id", "source_id": identifier}
        path = self.paths[identifier]
        directory = identifier == 0 or identifier > self.file_count
        binding = {"source_id": identifier, "resolved_path": path,
                   "source_kind": "directory" if directory else "file"}
        if directory and tool["name"] not in {"search_source", "list_sources", "project_info", "run_tests"}:
            return {"ok": False, "error": "source_file_id_required", **binding}
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "source_binding.json").write_text(json.dumps(binding, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {**self._execute_path({**tool, "path": path}, output), **binding,
                **({"provenance": "base_commit", "base_commit": self.base_commit} if self.evidence else {})}

    def _execute_path(self, tool, output):

        name, path = tool["name"], tool.get("path", "")
        if self.evidence and name == "list_sources":
            choices = [p for p in self.paths[1:self.file_count + 1]
                       if (not path or p == path or p.startswith(path.rstrip("/") + "/")) and tool["query"] in p]
            offset = max(0, tool["start_line"])
            return {"ok": True, "files": [{"source_id": self.ids[p], "file": p} for p in choices[offset:offset + 200]],
                    "total": len(choices), "next_offset": offset + 200 if offset + 200 < len(choices) else None}
        if self.evidence and name in {"project_info", "run_tests"}:
            from .browser_agent import discover_test_runner, run_base_test
            runner = discover_test_runner(self.repo)
            if name == "project_info":
                package = json.loads(self.index["package.json"]["text"]) if "package.json" in self.index else {}
                return {"ok": True, "scripts": package.get("scripts", {}), "test_runner": runner,
                        "test_sources": [{"source_id": self.ids[p], "file": p} for p in self.index
                                         if set(PurePosixPath(p).parts) & {"test", "tests", "__tests__", "spec", "specs"}][:200]}
            return run_base_test(self.repo, runner, path, output)
        if name not in {"read_source", "search_source", "dependencies", "symbols"}:
            return {"ok": False, "error": "source_tool_unavailable"}
        directory_prefix = path.rstrip("/") + "/"
        search_directory = name == "search_source" and any(file.startswith(directory_prefix) for file in self.index)
        if path and path not in self.index and path != "package.json" and not search_directory:
            return {"ok": False, "error": "path_not_in_base_source_index", "path": path}
        if path and ((self.repo / path).is_symlink() or not (self.repo / path).resolve().is_relative_to(self.repo)):
            return {"ok": False, "error": "path_outside_base_source", "path": path}
        if name == "search_source":
            query = tool["query"]
            if not query:
                return {"ok": False, "error": "empty_literal_query"}
            matches = [{"source_id": self.ids[file], "file": file, "line": i, "text": line[:1000]}
                       for file, entry in self.index.items()
                       if not path or file == path or (search_directory and file.startswith(directory_prefix))
                       for i, line in enumerate(entry["lines"], 1) if query in line]
            offset = max(0, tool["start_line"]) if self.evidence else 0
            return {"ok": True, "matches": matches[offset:offset + 60], "total_matches": len(matches),
                    "truncated": offset + 60 < len(matches), "next_offset": offset + 60 if offset + 60 < len(matches) else None}
        if not path or not (self.repo / path).is_file():
            return {"ok": False, "error": "source_path_required"}
        if name == "read_source":
            data = self.index[path]["text"].encode("utf-8") if self.evidence else (self.repo / path).read_bytes()
            lines = data.decode("utf-8").splitlines(keepends=True)
            start = max(1, tool["start_line"])
            end = min(tool["end_line"] or start + 239, start + 239, len(lines))
            if start > end:
                return {"ok": False, "error": "line_window_outside_source", "total_lines": len(lines)}
            text = "".join(lines[start - 1:end])
            if path in self.editable_files and path not in self.read_files:
                self.read_files.append(path)
            return {"ok": True, "file": path, "start_line": start, "end_line": end,
                    "text": text[:24000], "truncated": len(text) > 24000 or end < len(lines),
                    "sha256": hashlib.sha256(data).hexdigest(), "total_lines": len(lines)}
        if name == "symbols":
            if not path.endswith(SCRIPT):
                return {"ok": False, "error": "symbols_require_script_source"}
            from .browser_agent import _ast
            return _ast(self.repo, self.repo / path, Path(output) / "ast")
        graph, unresolved, edges = _graph(self.index)
        return {"ok": True, "file": path, "neighbors": sorted(graph.get(path, [])),
                "neighbor_sources": [{"source_id": self.ids[target], "file": target, "relation": relation}
                                     for target, relation in sorted(graph.get(path, []))],
                "edges": [edge for edge in edges if path in (edge.get("from"), edge.get("to"))],
                "unresolved": unresolved.get(path, []), "static_only": True}


def _masked(text):

    return NONCODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)


def _index(repo, structure):

    indexed = {}

    def visit(tree, prefix=""):
        for name, entry in sorted(tree.items()):
            path = posixpath.join(prefix, name.replace("\\", "/"))
            if not isinstance(entry, dict):
                continue
            if "text" in entry:
                indexed[path] = dict(entry)
            elif name not in EXCLUDED:
                visit(entry, path)

    visit(structure)
    paths = set(indexed)
    for directory, dirs, files in os.walk(repo):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED and not (Path(directory) / name).is_symlink())
        paths.update((Path(directory) / name).relative_to(repo).as_posix()
                     for name in files if name.endswith(SCRIPT + VISUAL))
    result = {}
    for name in sorted(paths):
        path = repo / name
        if not name.endswith(SCRIPT + VISUAL) or not path.is_file() or path.is_symlink():
            continue
        if not path.resolve().is_relative_to(repo) or any((repo / parent).is_symlink() for parent in PurePosixPath(name).parents):
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        result[name] = {**indexed.get(name, {}), "text": text, "lines": text.splitlines(), "masked": _masked(text)}
    return result


def _ranges(entry):

    ranges = []
    for node in [*(entry.get("functions") or []), *(entry.get("classes") or [])]:
        if not isinstance(node, dict):
            continue
        for item in [node, *(node.get("methods") or [])]:
            start, end = item.get("start_line") or item.get("line"), item.get("end_line")
            if type(start) is int and type(end) is int and 1 <= start <= end <= len(entry["lines"]):
                ranges.append({"name": str(item.get("name", "")), "start": start, "end": end})
    return ranges


def _resolve(name, request, indexed):

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
    return next((candidate for candidate in candidates if candidate in indexed), None)


def _graph(indexed):

    neighbors, unresolved, edges = defaultdict(list), defaultdict(list), []
    directories = defaultdict(list)
    for name in indexed:
        directories[posixpath.dirname(name)].append(name)
    for name, entry in indexed.items():
        for match in IMPORT.finditer(entry["text"]):
            if not entry["masked"][match.start():match.start() + 2].strip():
                continue
            request = next(group for group in match.groups() if group is not None)
            target = _resolve(name, request, indexed)
            if target is None:
                unresolved[name].append({"from": name, "request": request, "reason": "unresolved_import"})
                continue
            kind = "visual_import" if target.endswith(VISUAL) else "import"
            edges.append({"from": name, "to": target, "kind": kind})
            neighbors[name].append((target, kind))
            neighbors[target].append((name, "consumer"))
        for target in directories[posixpath.dirname(name)]:
            if name != target and target.endswith(VISUAL) and not name.endswith(VISUAL):
                neighbors[name].append((target, "same_directory_visual"))
                neighbors[target].append((name, "same_directory_source"))
                edges.append({"from": name, "to": target, "kind": "same_directory_visual"})
    return neighbors, unresolved, edges


def _merge(intervals):

    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _slice(entry, hypotheses, issue):

    lines, ranges = entry["lines"], _ranges(entry)
    count = len(lines)
    if count <= MAX_LINES:
        selected = [(1, count)] if count else []
    else:
        anchors = []
        for hypothesis in hypotheses:
            symbol = hypothesis.get("symbol", "")
            matches = [item for item in ranges if item["name"] == symbol or item["name"].split(".")[-1] == symbol.split(".")[-1]] if symbol else []
            if matches:
                anchors.extend(item["start"] for item in matches)
            elif type(hypothesis.get("line")) is int and 1 <= hypothesis["line"] <= count:
                anchors.append(hypothesis["line"])
        if not anchors:
            words = {word.lower() for word in re.findall(r"[A-Za-z_$][\w$]{2,}", issue)}
            ranked = sorted(ranges, key=lambda item: (-len(words & set(re.findall(r"\w+", item["name"].lower()))), item["start"]))
            anchors = [item["start"] for item in ranked[:2]] or [1]
        wanted = []
        for line in dict.fromkeys(anchors):
            containers = [item for item in ranges if item["start"] <= line <= item["end"]]
            fitting = [item for item in containers if item["end"] - item["start"] + 1 <= MAX_LINES // 2]
            if fitting:
                outer = max(fitting, key=lambda item: item["end"] - item["start"])
                wanted.append((outer["start"], outer["end"]))
            else:
                wanted.append((max(1, line - 40), min(count, line + 100)))
        seed_text = "\n".join("\n".join(lines[start - 1:end]) for start, end in wanted)
        called = set(re.findall(r"\b([$A-Za-z_][$\w]*)\s*\(", _masked(seed_text)))
        helpers = set()
        while True:
            reached = [item for item in ranges if item["name"].split(".")[-1] in called
                       and (item["start"], item["end"]) not in helpers]
            if not reached:
                break
            for item in reached:
                helpers.add((item["start"], item["end"]))
                wanted.append((item["start"], item["end"]))
                body = "\n".join(lines[item["start"] - 1:item["end"]])
                called.update(re.findall(r"\b([$A-Za-z_][$\w]*)\s*\(", _masked(body)))
        occupied = _merge([(item["start"], item["end"]) for item in ranges])
        cursor = 1
        outer_context = []
        for start, end in occupied:
            if cursor < start:
                outer_context.append((cursor, start - 1))
            cursor = end + 1
        if cursor <= count:
            outer_context.append((cursor, count))
        wanted = [wanted[0], (1, min(50, count)), *outer_context, *wanted[1:]]
        selected = []
        for start, end in wanted:
            candidate = _merge([*selected, (start, end)])
            while len(candidate) > 4:
                at = min(range(len(candidate) - 1), key=lambda index: candidate[index + 1][0] - candidate[index][1])
                candidate[at:at + 2] = [(candidate[at][0], candidate[at + 1][1])]
            if sum(end - start + 1 for start, end in candidate) <= MAX_LINES:
                selected = candidate
    windows = [{"start_line": start, "end_line": end, "text": "\n".join(lines[start - 1:end])} for start, end in selected]
    omitted, cursor = [], 1
    for start, end in selected:
        if cursor < start:
            omitted.append({"start_line": cursor, "end_line": start - 1})
        cursor = end + 1
    if cursor <= count:
        omitted.append({"start_line": cursor, "end_line": count})
    return {"windows": windows, "complete": not omitted, "omitted": omitted,
            "index_kind": "baseline_ast" if ranges else "text_only", "total_lines": count,
            "ast_ranges_available": bool(ranges)}


def _binding(name, entry, hypothesis, context, dependencies):

    symbol = hypothesis.get("symbol", "")
    ranges = _ranges(entry)
    matched = [item for item in ranges if item["name"] == symbol]
    if not matched and symbol:
        matched = [item for item in ranges if item["name"].split(".")[-1] == symbol.split(".")[-1]]
    declared = set(DECLARATION.findall(entry["masked"]))
    declared.update(item["name"] for item in ranges)
    symbol_exists = True if symbol and (matched or symbol in declared) else None
    if symbol and not re.search(r"(?<![\w$])" + re.escape(symbol.split(".")[-1]) + r"(?![\w$])", entry["masked"]):
        symbol_exists = False
    readable = set()
    selected_text = "\n".join(window["text"] for window in context["windows"])
    readable.update(DECLARATION.findall(_masked(selected_text)))
    for item in matched:
        header = "\n".join(entry["lines"][item["start"] - 1:min(item["start"] + 3, item["end"])])
        params = re.search(r"\(([^()]*)\)", header)
        if params:
            readable.update(re.findall(r"(?:^|,)\s*([$A-Za-z_][$\w]*)", params.group(1)))
    effects = []
    property_name = hypothesis.get("target_property", "").split(".")[-1]
    if re.fullmatch(r"[$A-Za-z_][$\w]*", property_name or ""):
        assignment = re.compile(r"(?<![\w$])" + re.escape(property_name) + r"\s*(?:[+*/-]?=(?!=)|:)")
        call = re.compile(r"(?<![\w$])" + re.escape(property_name) + r"\s*\(")
        for number, line in enumerate(entry["masked"].splitlines(), 1):
            if matched and not any(item["start"] <= number <= item["end"] for item in matched):
                continue
            if assignment.search(line) or call.search(line):
                effects.append({"file": name, "line": number, "text": entry["lines"][number - 1],
                                "kind": "property_write" if assignment.search(line) else "call",
                                "static_only": True, "target_binding_verified": False})
    requested = set(hypothesis.get("readable_symbols") or [])
    reasons = [reason for condition, reason in [
        (not context["complete"], "omitted_source_ranges"), (bool(dependencies), "unresolved_dependency_closure"),
        (not matched, "boundary_not_ast_bound"), (not requested <= readable, "requested_symbols_not_declared_in_context"),
        (True, "lexical_scope_and_dynamic_dispatch_not_proven"),
    ] if condition]
    return {"symbol_exists": symbol_exists, "readable_symbols": sorted(readable),
            "readable_symbols_basis": "declaration_candidates_without_accessibility_proof",
            "downstream_summary": {"status": "partial_static", "source_effects": effects, "unknown_reasons": reasons},
            "effect_evidence": effects, "interface_complete": False,
            "requested_interface_covered": bool(requested) and requested <= readable and context["complete"] and not dependencies,
            "interface_basis": "declaration_candidates_without_lexical_scope_proof", "ast_boundaries": matched}


def build_scope(repo: Path, structure: dict, topk: list[str], hypotheses: list[dict],
                issue: str, max_files: int = 10) -> dict:

    repo = Path(repo).resolve()
    indexed = _index(repo, structure)
    neighbors, unresolved, all_edges = _graph(indexed)
    seeds = list(dict.fromkeys(name for name in topk if name in indexed))
    selected = seeds[:max_files]
    additions = list(dict.fromkeys(hypothesis.get("file") for hypothesis in hypotheses if hypothesis.get("file") in indexed))
    for name in additions:
        if name not in selected and len(selected) < max_files:
            selected.append(name)
    queue, visited = deque(selected), set(selected)
    while queue:
        source = queue.popleft()
        for target, kind in sorted(neighbors[source], key=lambda edge: (edge[1].startswith("same_directory"), edge[0], edge[1])):
            if target in visited:
                continue
            visited.add(target)
            if len(selected) < max_files:
                selected.append(target)
                queue.append(target)
    context, dependencies = [], []
    for name in selected:
        dependencies.extend(unresolved[name])
        for target, kind in neighbors[name]:
            if target not in selected:
                dependencies.append({"from": name, "request": target, "reason": "file_budget", "kind": kind})
        item = _slice(indexed[name], [hypothesis for hypothesis in hypotheses if hypothesis.get("file") == name], issue)
        context.append({"file": name, **item})
    for name in topk:
        if name not in selected:
            dependencies.append({"from": name, "request": name, "reason": "topk_not_indexed" if name not in indexed else "file_budget"})
    by_name = {item["file"]: item for item in context}
    bindings = {}
    for hypothesis in hypotheses:
        name, identifier = hypothesis.get("file"), hypothesis.get("id")
        if name in by_name:
            bindings[identifier] = _binding(name, indexed[name], hypothesis, by_name[name], dependencies)
        else:
            bindings[identifier] = {"symbol_exists": None, "readable_symbols": [], "downstream_summary": {"status": "unknown", "reason": "outside_context"}, "effect_evidence": [], "interface_complete": False}
    return {"context": context, "edges": [edge for edge in all_edges if edge["from"] in selected or edge["to"] in selected],
            "allowed_files": selected, "unresolved_dependencies": dependencies,
            "truncated": any(not item["complete"] for item in context) or any(item["reason"] == "file_budget" for item in dependencies),
            "bindings": bindings, "indexed_files": len(indexed), "visual_index_files": [name for name in indexed if name.endswith(VISUAL)],
            "baseline_topk": list(topk), "scope_limits": {"max_files": max_files, "max_windows_per_file": 4, "max_lines_per_file": MAX_LINES}}


S2_CONTEXT_DEFAULTS = {
    "max_window_utf8_bytes": 16384,
    "max_source_total_utf8_bytes": 65536,
    "max_payload_utf8_bytes": 131072,
    "context_radius": 8,
    "max_direct_helpers": 8,
    "max_dependency_files": 4,
    "fallback_max_files": 3,
    "fallback_max_windows_per_file": 2,
}


def s2_context_config(overrides=None):

    overrides = overrides or {}
    assert not set(overrides) - set(S2_CONTEXT_DEFAULTS), "Unknown S2 context setting"
    config = {**S2_CONTEXT_DEFAULTS, **overrides}
    assert all(type(value) is int and value > 0 for value in config.values()), "Invalid S2 context limit"
    assert config["max_window_utf8_bytes"] <= config["max_source_total_utf8_bytes"] <= config["max_payload_utf8_bytes"]
    return config


def _s2_size(value):

    text = json.dumps(value, ensure_ascii=False)
    return {"characters": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def project_issue_context(issue, *, max_utf8_bytes, window_utf8_bytes, focus):

    raw = issue.encode("utf-8")
    audit = {"protocol": "issue_blocks_v1", "original_sha256": hashlib.sha256(raw).hexdigest(),
             "original_utf8_bytes": len(raw), "original_reference": "input_context/task.json:problem_statement"}
    if _s2_size(issue)["utf8_bytes"] <= max_utf8_bytes:
        return issue, {**audit, "status": "full", "omissions": []}
    lines = issue.splitlines(keepends=True)
    blocks, texts, offset, fence = [], {}, 0, None
    current = []

    def emit(kind, start, end, text, summary=None):
        nonlocal offset
        data = text.encode("utf-8")
        identifier = f"Q{len(blocks) + 1}"
        blocks.append({"id": identifier, "kind": kind, "start_line": start, "end_line": end,
                       "start_byte": offset, "end_byte": offset + len(data),
                       "sha256": hashlib.sha256(data).hexdigest(),
                       **({"summary": summary} if summary else {})})
        texts[identifier] = text
        offset += len(data)

    def flush():
        if current:
            emit(current[0][0], current[0][1], current[-1][1], "".join(row[2] for row in current))
            current.clear()

    for number, line in enumerate(lines, 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        kind = "code" if fence else "prose"
        if marker and fence is None:
            fence, kind = marker.group(1), "code"
        elif marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not marker.group(2).strip():
            fence = None
        link = re.fullmatch(r" {0,3}\[([^\]\r\n]+)\]\((https?://[^\r\n]+)\)[ \t]*(?:\r?\n)?", line) if kind == "prose" else None
        if link and len(line.encode("utf-8")) > window_utf8_bytes:
            flush()
            url = urlsplit(link.group(2))
            emit("embedded_url", number, number, line, {"label": link.group(1),
                 "base_url": url._replace(query="", fragment="").geturl(),
                 "note": "Query/fragment payload is archived; it is not assumed equivalent to displayed code."})
            continue
        if current and (current[0][0] != kind or
                        (kind == "code" and sum(len(row[2].encode("utf-8")) for row in current) + len(line.encode("utf-8")) > window_utf8_bytes)):
            flush()
        current.append((kind, number, line))
    flush()
    assert offset == len(raw)
    words = set(re.findall(r"[A-Za-z_$][\w$]{2,}", focus.lower()))
    value = {**audit, "status": "excerpted", "blocks": []}
    for block in blocks:
        required = block["kind"] == "prose"
        value["blocks"].append({**block, "included": required,
                                **({"text": texts[block["id"]]} if required else {"omission": "issue_budget"})})
    if _s2_size(value)["utf8_bytes"] > max_utf8_bytes:
        return None, {**audit, "status": "required_issue_context_exceeds_budget",
                      "needed_utf8_bytes": _s2_size(value)["utf8_bytes"], "blocks": blocks}
    optional = [block for block in value["blocks"] if not block["included"]]
    ranked = sorted(optional, key=lambda block: (
        block["kind"] == "embedded_url",
        -len(words & set(re.findall(r"[A-Za-z_$][\w$]{2,}", texts[block["id"]].lower()))),
        block["start_byte"]))
    for block in ranked:
        text = texts[block["id"]]
        if len(text.encode("utf-8")) > window_utf8_bytes:
            block["omission"] = "issue_window_budget"
            continue
        block.update(included=True, text=text)
        block.pop("omission")
        if _s2_size(value)["utf8_bytes"] > max_utf8_bytes:
            block.pop("text")
            block.update(included=False, omission="issue_budget")
    omissions = [{key: block[key] for key in ("id", "kind", "start_line", "end_line", "start_byte", "end_byte", "sha256", "omission")}
                 for block in value["blocks"] if not block["included"]]
    assert _s2_size(value)["utf8_bytes"] <= max_utf8_bytes
    return value, {**audit, "status": "excerpted", "blocks": blocks, "omissions": omissions,
                   "projected_utf8_bytes": _s2_size(value)["utf8_bytes"]}


def _context_source(repo, name, expected_hash, gap):

    path = repo / name
    if (not name or "\\" in name or ":" in name or PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or not path.resolve().is_relative_to(repo)
            or any((repo / parent).is_symlink() for parent in [PurePosixPath(name), *PurePosixPath(name).parents])):
        gap("invalid_reference", file=name, reason="invalid_source_path")
        return None
    if not path.is_file() or not os.access(path, os.R_OK):
        gap("unreadable_file", file=name, reason="file_missing_or_unreadable")
        return None
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_hash:
        gap("source_version_mismatch", file=name, reason="indexed_source_hash_mismatch")
        return None
    text = raw.decode("utf-8", errors="replace")
    if text.encode("utf-8") != raw:
        gap("not_read", file=name, reason="source_not_valid_utf8")
        return None
    return {"text": text, "lines": text.splitlines(keepends=True), "sha256": digest}


def _s2_windows(sources, needs, limit):

    by_file = defaultdict(list)
    for need in needs:
        by_file[need["file"]].append(need)
    windows = []
    for name in sorted(by_file):
        source, requests = sources[name], by_file[name]
        for start, end in _merge([(item["start"], item["end"]) for item in requests]):
            cursor, used = start, 0
            for line in range(start, end + 1):
                size = len(source["lines"][line - 1].encode("utf-8"))
                assert size <= limit
                if used + size > limit:
                    windows.append((name, cursor, line - 1))
                    cursor, used = line, 0
                used += size
            windows.append((name, cursor, end))
    result = []
    for name, start, end in windows:
        requests = [item for item in by_file[name] if item["start"] <= end and start <= item["end"]]
        result.append({"id": f"W{len(result) + 1}", "file": name,
                       "source_sha256": sources[name]["sha256"], "start_line": start, "end_line": end,
                       "purposes": sorted({item["purpose"] for item in requests}),
                       "text": "".join(sources[name]["lines"][start - 1:end])})
    return result


def s2_context_metrics(payload, selected_boundaries):

    windows = payload["source_windows"]
    coverage = []
    for boundary in selected_boundaries:
        intervals = _merge([(w["start_line"], w["end_line"]) for w in windows if w["file"] == boundary["file"]])
        coverage.append({"boundary_id": boundary.get("boundary_id", boundary.get("id")),
                         "file": boundary["file"], "line": boundary["line"], "end_line": boundary["end_line"],
                         "start_covered": any(start <= boundary["line"] <= end for start, end in intervals),
                         "range_covered": any(start <= boundary["line"] and boundary["end_line"] <= end for start, end in intervals)})
    lines = [(w["file"], w["source_sha256"], line) for w in windows for line in range(w["start_line"], w["end_line"] + 1)]
    return {"payload": _s2_size(payload), "source_windows": len(windows),
            "source_characters": sum(len(w["text"]) for w in windows),
            "source_utf8_bytes": sum(len(w["text"].encode("utf-8")) for w in windows),
            "duplicate_source_lines": len(lines) - len(set(lines)), "coverage": coverage,
            "selected_targets": len(coverage), "covered_starts": sum(row["start_covered"] for row in coverage),
            "covered_ranges": sum(row["range_covered"] for row in coverage), "actual_input_tokens": None}


def build_hypothesis_payload(repo, *, issue, repo_name, base_commit, spec, front_context, topk,
                             s0_facts, budget=None):

    from copy import deepcopy
    from .front import _function_at, imported_symbols, export_names

    config, repo = s2_context_config(budget), Path(repo).resolve()
    audit = {"protocol": "s0_to_s2_v1", "status": "building", "config": config,
             "reference_checks": deepcopy(s0_facts.get("reference_checks", [])), "omissions": [],
             "deduplicated": {"constraints": 0, "boundaries": 0, "interfaces": 0, "notes": 0}, "metrics": {}}
    payload = {"protocol": audit["protocol"], "task": {"issue": issue, "repo": repo_name, "base_commit": base_commit},
               "entities": list(dict.fromkeys(spec["entities"])), "constraints": [], "ambiguity_groups": [],
               "interfaces": [], "selected_boundaries": [], "upstream_notes": [], "source_windows": [],
               "gaps": [], "coverage": {"required_targets_complete": False, "source_scope": "windows_only"}}
    files, sources, needs = s0_facts["files"], {}, []
    gap_keys, constraint_index, selected, plans = set(), {}, {}, {}

    def gap(kind, **details):
        record = {"kind": kind, **details}
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key not in gap_keys:
            gap_keys.add(key)
            payload["gaps"].append(record)

    def fail(status, **details):
        audit.update(status=status, failure=details, gaps=deepcopy(payload["gaps"]))
        audit["metrics"] = s2_context_metrics(payload, list(selected.values()))
        audit["metrics"]["omitted_items"] = len(audit["omissions"])
        return None, audit

    if not base_commit or s0_facts.get("base_commit") != base_commit:
        return fail("base_version_mismatch")
    for item in spec["constraints"]:
        identifier = item["id"]
        if identifier in constraint_index:
            if item != constraint_index[identifier]:
                return fail("constraint_id_conflict", constraint_id=identifier)
            audit["deduplicated"]["constraints"] += 1
            continue
        constraint_index[identifier] = item
        projected = {key: item[key] for key in ("id", "role", "entity", "property", "context", "relation",
                                               "strength", "ambiguity_reason", "provenance_valid")}
        provenance = item["provenance"]
        projected["provenance"] = {key: provenance[key] for key in ("kind", "reference")}
        if provenance["kind"] == "image_region":
            projected["provenance"]["region"] = list(provenance["region"])
        payload["constraints"].append(projected)
    for group in spec.get("ambiguity_groups", []):
        if not set(group["constraint_ids"]) <= constraint_index.keys():
            return fail("invalid_ambiguity_reference", constraint_ids=group["constraint_ids"])
        value = {"constraint_ids": list(dict.fromkeys(group["constraint_ids"])), "reason": group["reason"]}
        if value not in payload["ambiguity_groups"]:
            payload["ambiguity_groups"].append(value)
    for plan in front_context["scope_plan"].get("boundary_plans", []):
        if plan["id"] not in plans:
            plans[plan["id"]] = plan
    for origin, messages in [("S0_boundaries", s0_facts.get("boundary_unresolved", [])),
                              ("S0_scope", front_context["scope_plan"].get("unresolved", []))]:
        for message in messages:
            gap("localization_unresolved", origin=origin, text=message)

    def source(name):
        if name in sources:
            return sources[name]
        path = repo / name
        if (not name or "\\" in name or ":" in name or PurePosixPath(name).is_absolute()
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or not path.resolve().is_relative_to(repo)
                or any((repo / parent).is_symlink() for parent in [PurePosixPath(name), *PurePosixPath(name).parents])):
            gap("invalid_reference", file=name, reason="invalid_source_path")
            sources[name] = None
            return None
        if name not in files:
            gap("not_read", file=name, reason="outside_source_index")
            sources[name] = None
            return None
        sources[name] = _context_source(repo, name, files[name].get("source_sha256"), gap)
        return sources[name]

    def need(name, start, end, purpose, *, required=False, boundary_id=""):
        value = source(name)
        if value is None:
            return
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(value["lines"]):
            gap("invalid_reference", file=name, reason="line_out_of_range", start_line=start, end_line=end)
            return
        record = {"file": name, "start": start, "end": end, "purpose": purpose,
                  "required": required, "boundary_id": boundary_id}
        if record not in needs:
            needs.append(record)

    interface_keys, note_keys = {}, {}

    def note(kind, origin, text):
        key = (kind, origin, text)
        if key in note_keys:
            audit["deduplicated"]["notes"] += 1
            return note_keys[key]
        identifier = f"N{len(note_keys) + 1}"
        note_keys[key] = identifier
        payload["upstream_notes"].append({"id": identifier, "kind": kind, "origin": origin, "text": text})
        return identifier

    seen = {}
    for item in front_context["selected_boundaries"]:
        identifier = item["id"]
        if identifier in seen:
            if item != seen[identifier]:
                return fail("boundary_id_conflict", boundary_id=identifier)
            audit["deduplicated"]["boundaries"] += 1
            continue
        seen[identifier] = item
        boundary = s0_facts["boundary_index"].get(identifier)
        if boundary is None or boundary["id"] != identifier:
            gap("invalid_reference", boundary_id=identifier, reason="unknown_boundary_id")
            continue
        name, line, end = boundary["file"], boundary["line"], boundary["end_line"]
        value = source(name)
        if value is None:
            gap("not_read", boundary_id=identifier, file=name, reason="selected_source_unavailable")
            continue
        if type(line) is not int or type(end) is not int or not 1 <= line <= end <= len(value["lines"]):
            gap("invalid_reference", boundary_id=identifier, file=name, reason="line_out_of_range")
            continue
        for key in ("file", "line", "end_line", "mechanism", "expression"):
            if item.get(key) != boundary[key]:
                audit["reference_checks"].append({"boundary_id": identifier, "field": key, "reason": "program_value_used"})
        refs = []
        for cid in item["K_b"]["constraint_ids"]:
            if cid in constraint_index:
                if cid not in refs:
                    refs.append(cid)
            else:
                gap("invalid_reference", boundary_id=identifier, constraint_id=cid, reason="unknown_constraint_id")
        plan = plans.get(identifier, {})
        reads = list(dict.fromkeys(boundary["read_symbols"]))
        focused = [symbol for symbol in dict.fromkeys(plan.get("edit_reads", [])) if symbol in reads]
        for symbol in plan.get("edit_reads", []):
            if symbol not in reads:
                gap("invalid_reference", boundary_id=identifier, symbol=symbol, reason="outside_read_candidates")
        interface = {"edit_unit": boundary["edit_unit"], "readable_candidates": reads,
                     "read_basis": boundary["read_basis"], "focus_symbols": focused}
        key = json.dumps(interface, sort_keys=True)
        if key not in interface_keys:
            interface_keys[key] = f"I{len(interface_keys) + 1}"
            payload["interfaces"].append({"id": interface_keys[key], **interface})
        else:
            audit["deduplicated"]["interfaces"] += 1
        explanation = item["expressivity"]
        if explanation["verdict"] != "UNKNOWN" or explanation.get("certificate") or explanation.get("colliding_objects"):
            return fail("unsupported_expressivity_evidence", boundary_id=identifier)
        notes = [note("selection_reason", "S0_boundaries", item["selection_reason"])] if item["selection_reason"] else []
        for text in [*item["K_b"]["declared_preserve"], *plan.get("preserve", [])]:
            if text:
                ref = note("preserve_suggestion", "S0_scope", text)
                if ref not in notes:
                    notes.append(ref)
        projected = {"boundary_id": identifier, **{key: boundary[key] for key in ("file", "line", "end_line", "mechanism", "expression")},
                     "interface_id": interface_keys[key], "constraint_ids": refs, "note_ids": notes,
                     "source_window_ids": [], "expressivity": {key: explanation[key] for key in ("verdict", "reason")}}
        selected[identifier] = projected
        payload["selected_boundaries"].append(projected)
        entry = files[name]
        owner = _function_at(entry, line, boundary.get("span", [None])[0])
        target_end = line if boundary["edit_unit"] == "function_body" else end
        need(name, line, target_end, "target", required=True, boundary_id=identifier)
        need(name, max(1, line - config["context_radius"]), min(len(value["lines"]), target_end + config["context_radius"]),
             "local_context", boundary_id=identifier)
        if owner:
            bodies = [scope for scope in entry.get("scopes", []) if scope["kind"] == "function_body"
                      and owner["start"] <= scope["start"] < scope["end"] <= owner["end"]]
            header_end = min(bodies, key=lambda scope: scope["start"])["line"] if bodies else owner["line"]
            need(name, owner["line"], header_end, "function_signature", required=True, boundary_id=identifier)
        target_text = "".join(value["lines"][line - 1:target_end])
        referenced = set(re.findall(r"[$A-Za-z_][$\w]*", _masked(target_text))) & set(reads)
        defined = set(owner.get("params", []) if owner else []) | {"this"}
        import_bindings = entry.get("import_bindings")
        if import_bindings is None:
            import_bindings = imported_symbols(value["text"])
        defined.update(binding["local"].split(".")[0] for binding in import_bindings)
        for declaration in entry.get("declarations", []):
            if (declaration.get("name") in referenced and declaration["line"] <= target_end
                    and declaration.get("owner", 0) in {0, owner["line"] if owner else 0}):
                defined.add(declaration["name"])
                if declaration["line"] < line:
                    need(name, declaration["line"], declaration["end_line"], "local_definition", required=True, boundary_id=identifier)
        for symbol in sorted(referenced - defined):
            gap("no_evidence", boundary_id=identifier, symbol=symbol, reason="local_definition_not_resolved")
        for obligation in item["K_b"]["static_obligations"]:
            need(obligation["file"], obligation["line"], obligation["line"], "static_property_use", boundary_id=identifier)

    if not selected:
        gap("no_evidence", reason="no_valid_selected_boundary")
    words = {word.lower() for word in re.findall(r"[A-Za-z_$][\w$]{2,}", issue)}
    bound_constraints = {cid for item in selected.values() for cid in item["constraint_ids"]}
    if not selected or set(constraint_index) - bound_constraints:
        for name in list(dict.fromkeys(topk))[:config["fallback_max_files"]]:
            value = source(name)
            if value is None or not value["lines"]:
                continue
            ranges = files[name].get("functions", [])
            ranked = sorted(ranges, key=lambda node: (-len(words & set(re.findall(r"\w+", node["name"].lower()))), node["line"], node.get("start", 0)))
            anchors = [node["line"] for node in ranked[:config["fallback_max_windows_per_file"]]] or [1]
            for line in dict.fromkeys(anchors):
                if not selected:
                    need(name, line, line, "fallback_anchor", required=True)
                need(name, max(1, line - config["context_radius"]), min(len(value["lines"]), line + config["context_radius"]), "bounded_topk_exploration")

    initial_needs = list(needs)
    helper_count, dependency_files = 0, set()
    for name in dict.fromkeys(item["file"] for item in initial_needs):
        text = "\n".join("".join(sources[name]["lines"][item["start"] - 1:item["end"]]) for item in initial_needs if item["file"] == name)
        called = set(re.findall(r"(?<![\w$.])([$A-Za-z_][$\w]*)\s*\(", _masked(text)))
        for symbol in sorted(called):
            matches = [node for node in files[name].get("functions", []) if node["name"] == symbol]
            if len(matches) == 1 and any(item["required"] and item["file"] == name
                                       and matches[0]["line"] <= item["start"] <= item["end"] <= matches[0]["end_line"]
                                       for item in initial_needs):
                continue
            if len(matches) == 1 and helper_count < config["max_direct_helpers"]:
                node = matches[0]
                need(name, node["line"], node["end_line"], "direct_helper")
                helper_count += 1
            elif len(matches) > 1:
                gap("no_evidence", file=name, symbol=symbol, reason="ambiguous_helper_binding")
            elif matches:
                gap("not_read", file=name, symbol=symbol, reason="helper_limit")
        tokens = set(re.findall(r"[$A-Za-z_][$\w]*", _masked(text)))
        bindings = files[name].get("import_bindings")
        if bindings is None:
            bindings = imported_symbols(sources[name]["text"])
        targets = []
        for binding in bindings:
            if binding["local"].split(".")[0] not in tokens:
                continue
            target = _resolve(name, binding["request"], files)
            if target is None:
                gap("unresolved_dependency", file=name, request=binding["request"], symbol=binding["local"])
            else:
                targets.append((target, binding["imported"], "direct_import"))
        targets.extend((target, "", "same_directory_visual") for target in sorted(files)
                       if target.endswith(VISUAL) and posixpath.dirname(target) == posixpath.dirname(name))
        for target, symbol, reason in dict.fromkeys(targets):
            if target not in dependency_files and len(dependency_files) >= config["max_dependency_files"]:
                gap("not_read", file=target, reason="dependency_file_limit", related_to=name)
                continue
            value = source(target)
            if value is None or not value["lines"]:
                continue
            dependency_files.add(target)
            exported = export_names({"masked": _masked(value["text"])})
            names = set(exported.get(symbol, ())) | {symbol}
            definitions = [node for node in files[target].get("functions", []) if node["name"] in names]
            if definitions:
                for node in definitions[:config["max_direct_helpers"]]:
                    need(target, node["line"], node["end_line"], reason)
            else:
                need(target, 1, min(len(value["lines"]), config["context_radius"] * 2 + 1), reason)
                if symbol:
                    gap("no_evidence", file=target, symbol=symbol, reason="import_definition_not_ast_resolved")

    accepted = []
    for item in sorted(needs, key=lambda row: not row["required"]):
        text = "".join(sources[item["file"]]["lines"][item["start"] - 1:item["end"]])
        size = len(text.encode("utf-8"))
        line_size = max(len(line.encode("utf-8")) for line in sources[item["file"]]["lines"][item["start"] - 1:item["end"]])
        reason = "window_byte_budget" if line_size > config["max_window_utf8_bytes"] else ""
        if not reason:
            candidate = _s2_windows(sources, [*accepted, item], config["max_window_utf8_bytes"])
            if sum(len(w["text"].encode("utf-8")) for w in candidate) > config["max_source_total_utf8_bytes"]:
                reason = "source_total_byte_budget"
        if reason:
            omission = {"file": item["file"], "start_line": item["start"], "end_line": item["end"],
                        "purpose": item["purpose"], "reason": reason, "required": item["required"]}
            audit["omissions"].append(omission)
            if item["required"]:
                return fail("required_context_exceeds_budget", **omission, needed_utf8_bytes=size)
            gap("budget_omitted", **{key: omission[key] for key in ("file", "start_line", "end_line", "purpose", "reason")})
        else:
            accepted.append(item)

    def render():
        payload["source_windows"] = _s2_windows(sources, accepted, config["max_window_utf8_bytes"])
        for identifier, boundary in selected.items():
            requests = [item for item in accepted if item["boundary_id"] == identifier]
            boundary["source_window_ids"] = [w["id"] for w in payload["source_windows"] if any(
                item["file"] == w["file"] and item["start"] <= w["end_line"] and w["start_line"] <= item["end"] for item in requests)]
            intervals = _merge([(w["start_line"], w["end_line"]) for w in payload["source_windows"] if w["file"] == boundary["file"]])
            boundary["source_range_complete"] = any(start <= boundary["line"] and boundary["end_line"] <= end for start, end in intervals)

    render()
    issue_focus = json.dumps({"constraints": payload["constraints"], "boundaries": payload["selected_boundaries"]}, ensure_ascii=False)
    original_issue_size = _s2_size(issue)["utf8_bytes"]

    def fit_issue():

        payload["task"]["issue"] = issue
        available = config["max_payload_utf8_bytes"] - _s2_size(payload)["utf8_bytes"] + original_issue_size
        projected, issue_audit = project_issue_context(issue, max_utf8_bytes=available,
            window_utf8_bytes=config["max_window_utf8_bytes"], focus=issue_focus)
        audit["issue_context"] = issue_audit
        if projected is not None:
            payload["task"]["issue"] = projected
        return projected is not None

    fit_issue()
    while _s2_size(payload)["utf8_bytes"] > config["max_payload_utf8_bytes"]:
        optional = [item for item in accepted if not item["required"]]
        if not optional:
            return fail("required_context_exceeds_budget", reason="payload_byte_budget", needed_utf8_bytes=_s2_size(payload)["utf8_bytes"])
        item = optional[-1]
        accepted.remove(item)
        omission = {"file": item["file"], "start_line": item["start"], "end_line": item["end"], "purpose": item["purpose"], "reason": "payload_byte_budget"}
        audit["omissions"].append(omission)
        gap("budget_omitted", **omission)
        render()
        fit_issue()
    if not payload["source_windows"]:
        return fail("no_readable_source")
    metrics = s2_context_metrics(payload, list(selected.values()))
    payload["coverage"]["required_targets_complete"] = bool(selected) and len(selected) == len(seen) and all(row["start_covered"] for row in metrics["coverage"])
    audit.update(status="ready", source_versions={name: value["sha256"] for name, value in sources.items() if value is not None},
                 selected_input_ids=list(seen), accepted_boundary_ids=list(selected), gaps=deepcopy(payload["gaps"]))
    audit["audit_only_paths"] = ["spec.provenance_invalid", "spec.ambiguity_policy", "front_context.candidate_pool",
                                "front_context.scope_plan.ranked_files", "front_context.value_decision_family",
                                "source_context.bindings", "source_context.allowed_files", "source_context.visual_index_files",
                                "source_context.indexed_files", "source_context.scope_limits", "baseline_topk"]
    audit["source_coverage"] = []
    for name, value in sources.items():
        if value is None:
            continue
        ranges = _merge([(w["start_line"], w["end_line"]) for w in payload["source_windows"] if w["file"] == name])
        cursor, omitted = 1, []
        for start, end in ranges:
            if cursor < start:
                omitted.append({"start_line": cursor, "end_line": start - 1, "reason": "not_expanded_in_payload"})
            cursor = end + 1
        if cursor <= len(value["lines"]):
            omitted.append({"start_line": cursor, "end_line": len(value["lines"]), "reason": "not_expanded_in_payload"})
        audit["source_coverage"].append({"file": name, "source_sha256": value["sha256"], "total_lines": len(value["lines"]),
                                         "included_ranges": ranges, "unexpanded_ranges": omitted})
    audit["metrics"] = s2_context_metrics(payload, list(selected.values()))
    audit["metrics"].update(omitted_items=len(audit["omissions"]), deduplicated=audit["deduplicated"],
                            unexpanded_source_ranges=sum(len(row["unexpanded_ranges"]) for row in audit["source_coverage"]),
                            rejected_boundaries=len(seen) - len(selected))
    return payload, audit


AGENT_CONTEXT_DEFAULTS = {
    "max_window_utf8_bytes": 16384, "max_source_total_utf8_bytes": 65536,
    "max_payload_utf8_bytes": 524288, "max_initial_message_utf8_bytes": 16777216,
    "context_radius": 8, "max_direct_helpers": 8, "max_dependency_files": 4,
    "fallback_max_files": 3, "fallback_max_windows_per_file": 2,
}
CONSTRAINT_FIELDS = ("id", "kind", "role", "entity", "property", "context", "relation", "strength",
                     "ambiguity_reason", "provenance_valid")
HYPOTHESIS_FIELDS = ("id", "boundary_id", "file", "symbol", "line", "mechanism", "readable_symbols",
                     "target_property", "constraint_ids")


def agent_context_config(overrides=None):

    overrides = overrides or {}
    assert not set(overrides) - {"code", "browser"}, "Unknown agent context role"
    result = {}
    for agent in ("code", "browser"):
        values = overrides.get(agent, {})
        assert not set(values) - set(AGENT_CONTEXT_DEFAULTS), "Unknown agent context setting"
        config = {**AGENT_CONTEXT_DEFAULTS, **values}
        assert all(type(value) is int and value > 0 for value in config.values()), "Invalid agent context limit"
        assert (config["max_window_utf8_bytes"] <= config["max_source_total_utf8_bytes"]
                <= config["max_payload_utf8_bytes"] <= config["max_initial_message_utf8_bytes"])
        result[agent] = config
    return result


def _agent_catalog(tools, names):

    catalog = tools.catalog()
    result = {key: catalog[key] for key in ("protocol", "file_count", "max_id")}
    if not tools.evidence:
        result["directories"] = deepcopy(catalog["directories"])
        result["discovery"] = "complete_file_listing_and_literal_search"
    else:
        groups = {"": {"id": 0, "path": "", "files": []}}
        for name in sorted(set(names)):
            if name not in tools.ids or not 1 <= tools.ids[name] <= tools.file_count:
                continue
            parent, _, basename = name.rpartition("/")
            groups.setdefault(parent, {"id": tools.ids[parent], "path": parent, "files": []})
            groups[parent]["files"].append([tools.ids[name], basename])
        result.update(evidence_policy="base", base_commit=catalog["base_commit"],
                      directories=list(groups.values()), listing_page_size=200,
                      file_listing_truncated=sum(len(g["files"]) for g in groups.values()) < tools.file_count,
                      discovery="list_sources_root_id_0_with_literal_path_filter_and_zero_based_offset")
    result["starting_sources"] = [{"source_id": tools.ids[name], "file": name} for name in dict.fromkeys(names)
                                  if name in tools.ids and 1 <= tools.ids[name] <= tools.file_count]
    return result


def _agent_runtime(capabilities, agent, mode, preflight_root):

    keys = ("node_available", "dependencies_available", "modules", "availability_note")
    if agent == "browser":
        keys += ("profile", "chromium_available", "T0_available", "T1_available", "T2_available",
                 "seed", "viewport", "device_scale_factor")
    result = {key: deepcopy(capabilities[key]) for key in keys if key in capabilities}
    if "modules" in result:
        result["modules"] = {key: result["modules"][key] for key in ("browserify", "rollup", "esbuild", "webpack", "jsdom")
                             if key in result["modules"]}
    result["origin"] = "preflight_environment_checks_not_hypothesis_execution"
    result["babel_parser_available"] = bool(capabilities["babel_parser"]) if "babel_parser" in capabilities else None
    if agent == "browser":
        result["witness_mode"] = mode
        result["entry_load"] = {key: deepcopy(capabilities["entry_load"][key]) for key in ("returncode", "reason")
                                if key in capabilities.get("entry_load", {})}
        diagnostic = Path(preflight_root) / "entry_load/stderr.log" if preflight_root is not None else None
        if diagnostic is not None and diagnostic.is_file():
            raw = diagnostic.read_bytes()
            result["entry_load"]["stderr_excerpt"] = raw[:2048].decode("utf-8", errors="replace")
            result["entry_load"]["stderr_truncated"] = len(raw) > 2048
        elif result["entry_load"].get("returncode", 0):
            result["entry_load"]["diagnostic_status"] = "not_available_in_initial_context"
    return result


def agent_context_metrics(payload, images=()):

    windows = payload["source_windows"]
    lines = [(w["file"], w["source_sha256"], n) for w in windows for n in range(w["start_line"], w["end_line"] + 1)]
    coverage = [{"hypothesis_id": item["hypothesis_id"], "origin": item["origin"], "file": item["file"],
                 "line": item["line"], "status": item["status"],
                 "covered": any(w["file"] == item["file"] and w["start_line"] <= item["line"] <= w["end_line"]
                                for w in windows) if item["status"] == "valid" else False}
                for item in payload["location_checks"]]
    initial = {"role": "user", "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}, *images]}
    return {"payload": _s2_size(payload), "initial_message": _s2_size(initial),
            "source_windows": len(windows), "source_characters": sum(len(w["text"]) for w in windows),
            "source_utf8_bytes": sum(len(w["text"].encode("utf-8")) for w in windows),
            "duplicate_source_lines": len(lines) - len(set(lines)), "coverage": coverage,
            "valid_targets": sum(c["status"] == "valid" for c in coverage),
            "covered_targets": sum(c["covered"] for c in coverage),
            "invalid_references": sum(g["kind"] == "invalid_reference" for g in payload["gaps"]),
            "budget_omissions": sum(g["kind"] == "budget_omitted" for g in payload["gaps"]),
            "actual_input_tokens": None}


def build_code_agent_payload(repo, **arguments):

    return _build_agent_payload("code", repo, **arguments)


def build_browser_agent_payload(repo, **arguments):

    return _build_agent_payload("browser", repo, **arguments)


def _build_agent_payload(agent, repo, *, task, spec, raw_hypotheses, hypotheses, front_context, scope,
                         s0_facts, source_tools, runtime_capabilities, images=(), budget=None,
                         witness_mode="probe+render", preflight_root=None):

    from .front import _function_at, imported_symbols, export_names

    repo = Path(repo).resolve()
    config = agent_context_config({agent: budget or {}})[agent]
    payload = {"protocol": f"s2_to_{agent}_v1", "task": {key: task[key] for key in ("issue", "repo", "base_commit")},
               "requirements": {"entities": list(dict.fromkeys(spec["entities"])), "constraints": [], "ambiguity_groups": []},
               "hypotheses": [], "location_checks": [], "source_windows": [], "source_catalog": {},
               "issue_images": {"index_base": 0, "count": len(images), "indices": list(range(len(images)))},
               "gaps": [], "coverage": {"source_scope": "windows_only", "required_targets_complete": False, "fallback": False}}
    audit = {"protocol": payload["protocol"], "status": "building", "config": config,
             "reference_checks": [], "omissions": [], "source_versions": {}, "required_missing": []}
    sources, needs, seen_gaps, constraints, raw_by_id = {}, [], set(), {}, {}
    facts = s0_facts["files"]
    context = {"targets": [], "boundary_hints": [], "relations": [], "static_bindings": [], "upstream_notes": []}
    if agent == "code":
        payload["inspection_context"] = context
        payload["tool_environment"] = _agent_runtime(runtime_capabilities, agent, witness_mode, preflight_root)
    else:
        payload["hypothesis_context"] = context
        payload["runtime_context"] = _agent_runtime(runtime_capabilities, agent, witness_mode, preflight_root)
        payload["observation_targets"] = [{"hypothesis_id": h["id"], "question_ref": h["id"], "source_window_ids": []} for h in hypotheses]
        payload["reproduction_context"] = {"scenario_refs": {"issue": "task.issue", "constraint_ids": []},
                                           "entrypoints": [], "api_refs": []}

    def gap(kind, **details):
        record = {"kind": kind, **details}
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key not in seen_gaps:
            seen_gaps.add(key)
            payload["gaps"].append(record)

    def finish(status, **failure):
        audit.update(status=status, gaps=deepcopy(payload["gaps"]), metrics=agent_context_metrics(payload, images))
        if failure:
            audit["failure"] = failure
        return (payload if status in {"ready", "ready_with_gaps"} else None), audit

    if (not task["base_commit"] or s0_facts.get("base_commit") != task["base_commit"]
            or source_tools.evidence and source_tools.base_commit != task["base_commit"]):
        return finish("base_version_mismatch")
    for original in spec["constraints"]:
        identifier = original["id"]
        if identifier in constraints:
            if original != constraints[identifier]:
                return finish("constraint_id_conflict", constraint_id=identifier)
            continue
        constraints[identifier] = original
        item = {key: deepcopy(original[key]) for key in CONSTRAINT_FIELDS}
        item["provenance"] = {key: deepcopy(original["provenance"][key]) for key in ("kind", "reference")}
        if original["provenance"]["kind"] == "image_region":
            item["provenance"]["region"] = list(original["provenance"]["region"])
        payload["requirements"]["constraints"].append(item)
    for group in spec.get("ambiguity_groups", []):
        if not set(group["constraint_ids"]) <= constraints.keys():
            return finish("invalid_ambiguity_reference", constraint_ids=group["constraint_ids"])
        payload["requirements"]["ambiguity_groups"].append({key: deepcopy(group[key]) for key in ("constraint_ids", "reason")})
    for h in raw_hypotheses:
        if h["id"] in raw_by_id:
            return finish("hypothesis_id_conflict", hypothesis_id=h["id"])
        raw_by_id[h["id"]] = h
    if len({h["id"] for h in hypotheses}) != len(hypotheses):
        return finish("hypothesis_id_conflict")
    for h in hypotheses:
        item = {key: deepcopy(h[key]) for key in HYPOTHESIS_FIELDS}
        item["question"] = {key: h["question"][key] for key in ("kind", "expression", "expected")}
        item["boundary_valid"] = h.get("boundary_valid", False)
        payload["hypotheses"].append(item)
        for cid in h["constraint_ids"]:
            if cid not in constraints:
                gap("invalid_reference", hypothesis_id=h["id"], constraint_id=cid, reason="unknown_constraint_id")
        if h["id"] in raw_by_id:
            changed = {key: {"raw": deepcopy(raw_by_id[h["id"]].get(key)), "normalized": deepcopy(h.get(key))}
                       for key in HYPOTHESIS_FIELDS if raw_by_id[h["id"]].get(key) != h.get(key)}
            if changed:
                audit["reference_checks"].append({"hypothesis_id": h["id"], "normalized_fields": changed})
    if agent == "browser":
        payload["reproduction_context"]["scenario_refs"]["constraint_ids"] = list(constraints)

    def source(name):
        if name in sources:
            return sources[name]
        sources[name] = None
        if name not in source_tools.ids or not 1 <= source_tools.ids[name] <= source_tools.file_count:
            gap("not_read", file=name, reason="outside_tool_registry")
            return None
        entry = source_tools.index.get(name, {})
        digest = facts.get(name, {}).get("source_sha256") or entry.get("sha256")
        if digest is None and "text" in entry:
            digest = hashlib.sha256(entry["text"].encode("utf-8")).hexdigest()
        if digest is None and name == "package.json":
            if re.fullmatch(r"[a-f0-9]{40}", task["base_commit"]):
                result = subprocess.run(["git", "-C", str(repo), "show", task["base_commit"] + ":" + name], capture_output=True)
                if result.returncode:
                    gap("not_read", file=name, reason="base_blob_unavailable")
                    return None
                digest = hashlib.sha256(result.stdout).hexdigest()
            elif (repo / name).is_file():
                digest = hashlib.sha256((repo / name).read_bytes()).hexdigest()
        value = _context_source(repo, name, digest, gap)
        if value is not None and entry.get("sha256") not in {None, value["sha256"]}:
            gap("source_version_mismatch", file=name, reason="tool_registry_hash_mismatch")
            return None
        sources[name] = value
        if value is not None:
            audit["source_versions"][name] = value["sha256"]
        return value

    def need(name, start, end, purpose, *, required=False, hypothesis_id=""):
        value = source(name)
        if value is None or type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(value["lines"]):
            if value is not None:
                gap("invalid_reference", file=name, start_line=start, end_line=end, reason="line_out_of_range")
            if required:
                audit["required_missing"].append({"file": name, "start_line": start, "end_line": end, "purpose": purpose})
            return
        record = {"file": name, "start": start, "end": end, "purpose": purpose,
                  "required": required, "hypothesis_id": hypothesis_id}
        if record not in needs:
            needs.append(record)

    for h in hypotheses:
        positions = [("normalized", h)]
        original = raw_by_id.get(h["id"])
        if original and any(original[key] != h[key] for key in ("file", "line", "symbol")):
            positions.append(("model_original", original))
        for origin, position in positions:
            name, line, symbol = position["file"], position["line"], position["symbol"]
            value = source(name)
            valid = value is not None and type(line) is int and 1 <= line <= len(value["lines"])
            location = {"hypothesis_id": h["id"], "origin": origin, "file": name, "line": line, "symbol": symbol,
                        "source_id": source_tools.ids[name] if name in source_tools.ids and 1 <= source_tools.ids[name] <= source_tools.file_count else None,
                        "status": "valid" if valid else "invalid",
                        "source_window_ids": []}
            payload["location_checks"].append(location)
            if not valid:
                gap("invalid_reference", hypothesis_id=h["id"], file=name, line=line, origin=origin,
                    reason="line_out_of_range" if value is not None else "source_unavailable")
                registered_file = name in source_tools.ids and 1 <= source_tools.ids[name] <= source_tools.file_count
                if value is None and (registered_file or name in facts) and origin == "normalized":
                    audit["required_missing"].append({"file": name, "line": line, "purpose": "target"})
                continue
            need(name, line, line, "target", required=True, hypothesis_id=h["id"])
            need(name, max(1, line - config["context_radius"]), min(len(value["lines"]), line + config["context_radius"]),
                 "local_context", hypothesis_id=h["id"])
            entry = facts.get(name, {})
            owner = _function_at(entry, line) if entry.get("functions") else None
            target = {"hypothesis_id": h["id"], "origin": origin, "file": name, "line": line,
                      "source_id": source_tools.ids[name], "source_window_ids": [], "function": None}
            context["targets"].append(target)
            if owner:
                target["function"] = {key: owner[key] for key in ("name", "line", "end_line")}
                scopes = [s for s in entry.get("scopes", []) if s["kind"] == "function_body"
                          and owner["start"] <= s["start"] < s["end"] <= owner["end"]]
                end = min(scopes, key=lambda s: s["start"])["line"] if scopes else owner["line"]
                need(name, owner["line"], end, "function_signature", required=True, hypothesis_id=h["id"])
                if symbol and owner["name"] and symbol.split(".")[-1] != owner["name"].split(".")[-1]:
                    gap("no_evidence", hypothesis_id=h["id"], file=name, symbol=symbol, reason="symbol_location_conflict")
            else:
                gap("no_evidence", hypothesis_id=h["id"], file=name, reason="function_context_unresolved")
            tokens = set(re.findall(r"[$A-Za-z_][$\w]*", _masked(value["lines"][line - 1])))
            for declaration in entry.get("declarations", []):
                if (declaration.get("name") in tokens and declaration["line"] < line
                        and declaration.get("owner", 0) in {0, owner["line"] if owner else 0}):
                    need(name, declaration["line"], declaration["end_line"], "local_definition", required=True, hypothesis_id=h["id"])

    for h in hypotheses:
        binding = scope.get("bindings", {}).get(h["id"], {})
        projected = {"hypothesis_id": h["id"], "origin": "legacy_scope_static_projection",
                     "source_range_basis": "legacy_scope_not_initial_agent_windows"}
        for key in ("symbol_exists", "readable_symbols", "readable_symbols_basis", "interface_complete",
                    "requested_interface_covered", "interface_basis"):
            if key in binding:
                projected[key] = deepcopy(binding[key])
        projected["downstream_summary"] = {key: deepcopy(binding["downstream_summary"][key])
            for key in ("status", "reason", "unknown_reasons") if key in binding.get("downstream_summary", {})}
        projected["effect_locations"] = []
        for effect in binding.get("effect_evidence", []):
            location = {key: deepcopy(effect[key]) for key in ("file", "line", "kind", "static_only", "target_binding_verified") if key in effect}
            location["source_window_ids"] = []
            projected["effect_locations"].append(location)
            need(effect["file"], effect["line"], effect["line"], "static_effect", hypothesis_id=h["id"])
        context["static_bindings"].append(projected)

    note_keys = set()
    for boundary in front_context.get("selected_boundaries", []):
        interface, expressivity = boundary["repair_interface"], boundary["expressivity"]
        context["boundary_hints"].append({"boundary_id": boundary["id"], "file": boundary["file"],
            "line": boundary["line"], "end_line": boundary["end_line"], "mechanism": boundary["mechanism"],
            "edit_unit": interface["G_b"], "readable_candidates": list(interface["z_b"]), "read_basis": interface["read_basis"],
            "constraint_ids": list(boundary["K_b"]["constraint_ids"]),
            "expressivity": {key: expressivity[key] for key in ("verdict", "reason")}})
        for kind, text in [("selection_reason", boundary["selection_reason"]),
                           *[("preserve_suggestion", t) for t in boundary["K_b"]["declared_preserve"]]]:
            if text and (kind, text) not in note_keys:
                note_keys.add((kind, text))
                context["upstream_notes"].append({"kind": kind, "origin": "S0_interpretation", "text": text})
        for obligation in boundary["K_b"].get("static_obligations", []):
            need(obligation["file"], obligation["line"], obligation["line"], "static_property_use")
    for origin, messages in [("S0_boundaries", s0_facts.get("boundary_unresolved", [])),
                              ("S0_scope", front_context.get("scope_plan", {}).get("unresolved", []))]:
        for message in messages:
            gap("localization_unresolved", origin=origin, text=message)
    for plan in front_context.get("scope_plan", {}).get("boundary_plans", []):
        for text in plan.get("preserve", []):
            if text and ("preserve_suggestion", text) not in note_keys:
                note_keys.add(("preserve_suggestion", text))
                context["upstream_notes"].append({"kind": "preserve_suggestion", "origin": "S0_interpretation", "text": text})

    if not any(p["status"] == "valid" for p in payload["location_checks"]):
        payload["coverage"]["fallback"] = True
        gap("no_evidence", reason="no_valid_hypothesis_location")
        for name in scope.get("baseline_topk", [])[:config["fallback_max_files"]]:
            value = source(name)
            if value is None or not value["lines"]:
                continue
            functions = facts.get(name, {}).get("functions", [])
            words = set(re.findall(r"\w+", task["issue"].lower()))
            ranked = sorted(functions, key=lambda f: (-len(words & set(re.findall(r"\w+", f["name"].lower()))), f["line"]))
            for line in dict.fromkeys([f["line"] for f in ranked[:config["fallback_max_windows_per_file"]]] or [1]):
                need(name, line, line, "fallback_anchor", required=True)
                need(name, max(1, line - config["context_radius"]), min(len(value["lines"]), line + config["context_radius"]), "fallback_context")

    if agent == "browser" and "package.json" in source_tools.ids:
        package = source("package.json")
        if package is not None:
            metadata = json.loads(package["text"])
            for key in ("name", "main", "module", "browser", "type", "exports", "scripts"):
                for line, text in enumerate(package["lines"], 1):
                    if re.search(r'"' + key + r'"\s*:', text):
                        need("package.json", line, line, "entry_evidence", required=key in {"main", "module", "browser"})
            entries = [(key, metadata[key]) for key in ("main", "module", "browser") if isinstance(metadata.get(key), str)]
            if "main" not in metadata and runtime_capabilities.get("entry"):
                entries.append(("preflight_default", "index.js"))
            for key, entry_name in entries:
                name = posixpath.normpath(entry_name)
                if name.startswith(("../", "/")) or name in {".", ".."} or "\\" in name or ":" in name:
                    gap("invalid_reference", file=name, reason="invalid_package_entry")
                    continue
                readable = name in source_tools.ids and 1 <= source_tools.ids[name] <= source_tools.file_count
                payload["reproduction_context"]["entrypoints"].append({"file": name, "basis": "package." + key,
                    "source_id": source_tools.ids[name] if readable else None, "source_readable": readable,
                    "source_window_ids": []})
                if readable:
                    value = source(name)
                    if value and value["lines"]:
                        need(name, 1, min(len(value["lines"]), 2 * config["context_radius"] + 1), "entry_source")
                else:
                    gap("not_read", file=name, reason="entry_not_in_source_registry")

    initial_needs, helpers, dependency_files = list(needs), 0, set()
    for name in dict.fromkeys(item["file"] for item in initial_needs):
        value = sources[name]
        text = "\n".join("".join(value["lines"][n["start"] - 1:n["end"]]) for n in initial_needs if n["file"] == name)
        entry = facts.get(name, {})
        called = set(re.findall(r"(?<![\w$.])([$A-Za-z_][$\w]*)\s*\(", _masked(text)))
        for symbol in sorted(called):
            matches = [f for f in entry.get("functions", []) if f["name"] == symbol]
            if len(matches) > 1:
                gap("no_evidence", file=name, symbol=symbol, reason="ambiguous_helper_binding")
            elif matches and helpers < config["max_direct_helpers"]:
                node = matches[0]
                if any(n["file"] == name and n["required"] and node["line"] <= n["start"] <= n["end"] <= node["end_line"] for n in initial_needs):
                    continue
                need(name, node["line"], node["end_line"], "direct_helper")
                helpers += 1
            elif matches:
                gap("not_read", file=name, symbol=symbol, reason="helper_limit")
        tokens = set(re.findall(r"[$A-Za-z_][$\w]*", _masked(text)))
        bindings = entry.get("import_bindings")
        bindings = imported_symbols(value["text"]) if bindings is None else bindings
        targets = []
        for binding in bindings:
            if binding["local"].split(".")[0] not in tokens:
                continue
            target = _resolve(name, binding["request"], source_tools.index)
            if target is None:
                gap("unresolved_dependency", file=name, request=binding["request"], symbol=binding["local"])
            else:
                targets.append((target, binding["imported"], "direct_import"))
        targets.extend((other, "", "same_directory_visual") for other in sorted(source_tools.index)
                       if other.endswith(VISUAL) and posixpath.dirname(other) == posixpath.dirname(name) and other != name)
        for target, symbol, relation in dict.fromkeys(targets):
            context["relations"].append({"from": name, "to": target, "source_id": source_tools.ids[target],
                                         "symbol": symbol, "kind": relation, "static_only": True})
            if target not in dependency_files and len(dependency_files) >= config["max_dependency_files"]:
                gap("not_read", file=target, reason="dependency_file_limit", related_to=name)
                continue
            target_source = source(target)
            if target_source is None or not target_source["lines"]:
                continue
            dependency_files.add(target)
            names = set(export_names({"masked": _masked(target_source["text"])}).get(symbol, ())) | {symbol}
            definitions = [node for node in facts.get(target, {}).get("functions", []) if node["name"] in names]
            for node in definitions[:config["max_direct_helpers"]]:
                need(target, node["line"], node["end_line"], relation)
            if not definitions:
                need(target, 1, min(len(target_source["lines"]), config["context_radius"] * 2 + 1), relation)
                if symbol:
                    gap("no_evidence", file=target, symbol=symbol, reason="import_definition_not_ast_resolved")

    starting = list(dict.fromkeys([h["file"] for h in hypotheses] + scope.get("baseline_topk", [])
                                 + front_context.get("scope_plan", {}).get("ranked_files", [])))
    payload["source_catalog"] = _agent_catalog(source_tools, starting + [item["file"] for item in needs])
    if audit["required_missing"]:
        return finish("required_source_unavailable")
    accepted = []
    for item in sorted(needs, key=lambda row: not row["required"]):
        text = "".join(sources[item["file"]]["lines"][item["start"] - 1:item["end"]])
        size = len(text.encode("utf-8"))
        reason = "window_byte_budget" if size > config["max_window_utf8_bytes"] else ""
        if not reason:
            windows = _s2_windows(sources, [*accepted, item], config["max_window_utf8_bytes"])
            if sum(len(w["text"].encode("utf-8")) for w in windows) > config["max_source_total_utf8_bytes"]:
                reason = "source_total_byte_budget"
        if reason:
            omission = {"file": item["file"], "start_line": item["start"], "end_line": item["end"],
                        "purpose": item["purpose"], "required": item["required"], "reason": reason}
            audit["omissions"].append(omission)
            gap("budget_omitted", **omission)
            if item["required"]:
                return finish("required_context_exceeds_budget", **omission, needed_utf8_bytes=size)
        else:
            accepted.append(item)

    def render():
        payload["source_windows"] = _s2_windows(sources, accepted, config["max_window_utf8_bytes"])
        for window in payload["source_windows"]:
            window["source_id"] = source_tools.ids[window["file"]]
        effects = [e for b in context["static_bindings"] for e in b["effect_locations"]]
        for location in payload["location_checks"] + context["targets"] + effects:
            location["source_window_ids"] = [w["id"] for w in payload["source_windows"] if w["file"] == location["file"]
                                             and type(location["line"]) is int and w["start_line"] <= location["line"] <= w["end_line"]]
            owner = location.get("function")
            if owner:
                ranges = _merge([(w["start_line"], w["end_line"]) for w in payload["source_windows"] if w["file"] == location["file"]])
                location["function_range_complete"] = any(a <= owner["line"] and owner["end_line"] <= b for a, b in ranges)
        if agent == "browser":
            for observation in payload["observation_targets"]:
                observation["source_window_ids"] = list(dict.fromkeys(w for t in context["targets"]
                    if t["hypothesis_id"] == observation["hypothesis_id"] for w in t["source_window_ids"]))
            for entry in payload["reproduction_context"]["entrypoints"]:
                entry["source_window_ids"] = [w["id"] for w in payload["source_windows"] if w["file"] == "package.json"]
            payload["reproduction_context"]["api_refs"] = [{"hypothesis_id": h["id"], "symbol": h["symbol"],
                "source_window_ids": list(dict.fromkeys(w for t in context["targets"] if t["hypothesis_id"] == h["id"]
                                                       for w in t["source_window_ids"]))} for h in hypotheses]
        positions = payload["location_checks"]
        payload["coverage"]["required_targets_complete"] = bool(positions) and all(p["status"] == "valid" and p["source_window_ids"] for p in positions)
        payload["coverage"]["files"] = []
        for name in sorted({w["file"] for w in payload["source_windows"]}):
            intervals = _merge([(w["start_line"], w["end_line"]) for w in payload["source_windows"] if w["file"] == name])
            payload["coverage"]["files"].append({"file": name, "total_lines": len(sources[name]["lines"]),
                "included_ranges": intervals, "complete": intervals == [(1, len(sources[name]["lines"]))]})

    render()
    while (_s2_size(payload)["utf8_bytes"] > config["max_payload_utf8_bytes"]
           or agent_context_metrics(payload, images)["initial_message"]["utf8_bytes"] > config["max_initial_message_utf8_bytes"]):
        optional = [item for item in accepted if not item["required"]]
        if not optional:
            return finish("required_context_exceeds_budget", reason="payload_or_initial_message_byte_budget")
        item = optional[-1]
        accepted.remove(item)
        omission = {"file": item["file"], "start_line": item["start"], "end_line": item["end"],
                    "purpose": item["purpose"], "reason": "payload_or_initial_message_byte_budget", "required": False}
        audit["omissions"].append(omission)
        gap("budget_omitted", **omission)
        render()
    if not payload["source_windows"]:
        return finish("no_readable_source")
    audit["source_coverage"] = []
    for name, value in sorted(sources.items()):
        if value is None:
            continue
        intervals = _merge([(w["start_line"], w["end_line"]) for w in payload["source_windows"] if w["file"] == name])
        cursor, omitted = 1, []
        for start, end in intervals:
            if cursor < start:
                omitted.append([cursor, start - 1])
            cursor = end + 1
        if cursor <= len(value["lines"]):
            omitted.append([cursor, len(value["lines"])])
        audit["source_coverage"].append({"file": name, "source_sha256": value["sha256"],
            "included_ranges": intervals, "unexpanded_ranges": omitted, "total_lines": len(value["lines"])})
    audit["automatic_reads_are_agent_tool_calls"] = False
    audit["full_state_projection_only"] = True
    return finish("ready_with_gaps" if payload["gaps"] else "ready")
