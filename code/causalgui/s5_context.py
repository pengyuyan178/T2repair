from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .code_agent import _context_source, _s2_windows, _merge, _resolve, _masked, CONSTRAINT_FIELDS
from .feedback import encode, request_size, source_visibility
from .synthesis import PATCH_PROTOCOL, _safe_path


PROTOCOL = "s5_context_v1"
DEFAULTS = {
    "max_window_utf8_bytes": 16384, "max_source_total_utf8_bytes": 65536,
    "max_evidence_utf8_bytes": 131072, "max_payload_utf8_bytes": 262144,
    "max_request_text_utf8_bytes": 524288, "context_radius": 8,
    "max_direct_helpers": 8, "max_dependency_files": 4,
    "fallback_max_files": 3, "fallback_max_windows_per_file": 2,
}
HYPOTHESIS_FIELDS = ("id", "boundary_id", "boundary_valid", "file", "symbol", "line", "mechanism",
    "question", "readable_symbols", "target_property", "constraint_ids", "verdict", "baseline_rank",
    "input_rank", "local_influence_supported", "witness_count")
RUNTIME_FIELDS = ("tier", "execution_ok", "reached", "values", "scenario_complete", "target_loaded",
                  "reason", "errors", "evidence")


def s5_context_config(overrides=None):

    values = overrides or {}
    assert set(values) <= {*DEFAULTS, "protocol"}, "Unknown S5 context setting"
    assert values.get("protocol", PROTOCOL) == PROTOCOL
    config = {"protocol": PROTOCOL, **DEFAULTS, **values}
    assert all(type(config[k]) is int and config[k] > 0 for k in DEFAULTS)
    assert config["max_window_utf8_bytes"] <= config["max_source_total_utf8_bytes"] <= config["max_payload_utf8_bytes"]
    return config


def text_size(value):

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"characters": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def _select(value, keys):
    return {k: deepcopy(value[k]) for k in keys if k in value}


def _gap(audit, kind, **details):
    record = {"kind": kind, **details}
    if record not in audit["gaps"]:
        audit["gaps"].append(record)


def _core(state, audit):

    task, spec, plan, root = (state.get(k) for k in ("task", "spec", "plan", "root"))
    if not all(isinstance(v, dict) for v in (task, spec, plan, root)):
        return None, "core_state_missing"
    if any(not isinstance(task.get(k), str) or not task[k] for k in ("issue", "repo", "base_commit")):
        return None, "task_identity_missing"
    if (not isinstance(spec.get("constraints"), list) or not isinstance(root.get("hypotheses"), list)
            or any(not isinstance(plan.get(k), list) for k in ("allowed_change", "new_files", "must_co_edit"))
            or any(not isinstance(name, str) for k in ("allowed_change", "new_files") for name in plan[k])
            or not set(plan["new_files"]) <= set(plan["allowed_change"])):
        return None, "requirements_or_permissions_unavailable"
    constraints, by_id = [], {}
    for original in spec["constraints"]:
        if not isinstance(original, dict) or not isinstance(original.get("id"), str):
            return None, "invalid_constraint_identity"
        item = _select(original, (*CONSTRAINT_FIELDS, "provenance"))
        if item["id"] in by_id:
            if encode(by_id[item["id"]]) != encode(item):
                return None, "constraint_id_conflict"
            audit["deduplicated_constraints"] += 1
            continue
        by_id[item["id"]] = item
        constraints.append(item)
    contract = _select(plan, ("allowed_change", "new_files", "must_co_edit", "interface_obligations", "atomicity",
        "ranked_files", "unknown_policy", "scope_complete", "interface_obligations_status", "patch_protocol"))
    if contract.get("patch_protocol") != PATCH_PROTOCOL:
        return None, "patch_protocol_unavailable"
    for field in ("must_preserve", "must_satisfy", "may_explain"):
        members = plan.get(field)
        if not isinstance(members, list):
            return None, "requirement_classification_unavailable"
        ids = []
        for member in members:
            identifier = member.get("id") if isinstance(member, dict) else None
            if identifier not in by_id or encode(_select(member, (*CONSTRAINT_FIELDS, "provenance"))) != encode(by_id[identifier]):
                return None, "plan_constraint_conflict"
            if identifier not in ids:
                ids.append(identifier)
        contract[field] = ids
    hypotheses = []
    for h in root["hypotheses"]:
        if not isinstance(h, dict) or not isinstance(h.get("id"), str) or h["id"] in {v["id"] for v in hypotheses}:
            return None, "hypothesis_identity_conflict"
        item = _select(h, HYPOTHESIS_FIELDS)
        certificate = h.get("certificate", {})
        item["certificate"] = _select(certificate, ("kind", "expected_prediction", "refutation_scope",
                                                     "excludes_candidate", "proves_expressibility"))
        item["certificate"]["witness_refs"] = []
        item["source_refs"], item["evidence_refs"] = [], []
        for identifier in h.get("constraint_ids", []):
            if identifier not in by_id:
                _gap(audit, "invalid_reference", hypothesis_id=h["id"], constraint_id=identifier)
        hypotheses.append(item)
    return {"protocol": PROTOCOL, "mode": "DEGRADED" if audit["mode"] == "minimal" else "NORMAL",
        "task": {**_select(task, ("issue", "repo", "base_commit")),
                 "issue_images": {"index_base": 0, "count": len(state.get("images", []))}},
        "requirements": {"entities": deepcopy(spec.get("entities", [])), "constraints": constraints,
                         "ambiguity_groups": deepcopy(spec.get("ambiguity_groups", []))},
        "edit_contract": contract,
        "hypotheses": {**_select(root, ("all_unknown", "negative_reach_policy")), "items": hypotheses},
        "source_windows": [], "evidence": {}, "agents": {}, "gaps": []}, None


def _observations(state):

    for agent in ("code", "browser"):
        report = state.get("reports", {}).get(agent, {})
        for ordinal, observation in enumerate(report.get("observations", [])):
            if isinstance(observation, dict):
                yield agent, ordinal, observation


def _initial_windows(payload):

    if "source_windows" in payload:
        return payload["source_windows"]
    context = payload.get("source_context", {})
    context = context.get("context", []) if isinstance(context, dict) else context
    return [{"file": file["file"], **window} for file in context for window in file.get("windows", [])]


def _match_count(text, search):

    position, count = text.find(search), 0
    while position >= 0:
        count += 1
        position = text.find(search, position + 1)
    return count


def source_range_covered(windows, name, start, end):

    ranges = _merge([(w["start_line"], w["end_line"]) for w in windows if w["file"] == name])
    return any(a <= start and end <= b for a, b in ranges)


def _sources(repo, state, payload, audit, config, *, minimal):

    sources, anchors = {}, []
    facts = state.get("s0_facts", {}).get("files", {})

    def source(name):
        if name in sources:
            return sources[name]
        sources[name] = None
        if not _safe_path(repo, name):
            _gap(audit, "invalid_reference", file=name, reason="unsafe_source_path")
            return None
        process = subprocess.run(["git", "-C", str(repo), "ls-tree", "-z", "HEAD", "--", name], capture_output=True)
        if process.returncode or not process.stdout:
            _gap(audit, "not_read", file=name, reason="base_blob_unavailable")
            return None
        mode, kind, oid = process.stdout.split(b"\t", 1)[0].split()
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            _gap(audit, "not_read", file=name, reason="unsupported_source_mode")
            return None
        blob = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", oid.decode()], capture_output=True)
        if blob.returncode:
            _gap(audit, "not_read", file=name, reason="base_blob_unavailable")
            return None
        value = _context_source(repo, name, hashlib.sha256(blob.stdout).hexdigest(), lambda k, **d: _gap(audit, k, **d))
        sources[name] = value
        if value is not None:
            audit["source_versions"][name] = value["sha256"]
        return value

    def anchor(name, start, end, origin, priority, *, center=None, expected=None):
        if not isinstance(name, str):
            _gap(audit, "invalid_reference", origin=origin, reason="source_name_missing")
            return
        value = source(name)
        if value is None:
            return
        if expected and expected != value["sha256"]:
            _gap(audit, "source_version_conflict", file=name, origin=origin, expected_sha256=expected)
            return
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(value["lines"]):
            _gap(audit, "invalid_reference", file=name, origin=origin, start_line=start, end_line=end)
            return
        anchors.append({"file": name, "start": start, "end": end, "purpose": origin,
                        "priority": priority, "center": center})

    radius = config["context_radius"]
    for h in payload["hypotheses"]["items"]:
        anchor(h.get("file"), h.get("line"), h.get("line"), "hypothesis:" + h["id"], 0, center=h.get("line"))
    for agent, initial in state.get("initial_payloads", {}).items():
        if not isinstance(initial, dict):
            _gap(audit, "initial_context_unavailable", agent=agent)
            continue
        for index, window in enumerate(_initial_windows(initial)):
            if isinstance(window, dict):
                anchor(window.get("file"), window.get("start_line"), window.get("end_line"),
                       f"initial:{agent}:{index}", 2, expected=window.get("source_sha256"))
                anchor(window.get("file"), window.get("start_line"), window.get("start_line"),
                       f"initial_anchor:{agent}:{index}", 1, center=window.get("start_line"), expected=window.get("source_sha256"))
    for agent, ordinal, observation in _observations(state):
        tool, raw = observation.get("tool", {}), observation.get("result", {})
        if tool.get("name") != "read_source" or not raw.get("ok"):
            continue
        visibility = source_visibility(tool, raw)
        identity = observation.get("evidence_id", f"{agent}:{ordinal}")
        audit["tool_source_visibility"][identity] = visibility
        name, text, start = raw.get("file"), raw.get("text"), raw.get("start_line")
        if not isinstance(name, str) or not isinstance(text, str) or type(start) is not int:
            _gap(audit, "source_text_unavailable", evidence_id=identity)
            continue
        value = source(name)
        if value is None:
            continue
        offset = sum(len(line) for line in value["lines"][:max(0, start - 1)])
        if (raw.get("sha256") not in {None, value["sha256"]} or start < 1
                or value["text"][offset:offset + len(text)] != text):
            _gap(audit, "source_version_conflict", evidence_id=identity, file=name, reason="raw_text_not_base")
            continue
        audit["verified_reads"][identity] = {"file": name, "start_line": start,
            "end_line": start + len(text.splitlines(keepends=True)) - 1, "source_sha256": value["sha256"]}
        anchor(name, start, audit["verified_reads"][identity]["end_line"], "tool:" + identity, 2)
        anchor(name, start, start, "tool_anchor:" + identity, 1, center=start)
    for boundary in state.get("front_context", {}).get("selected_boundaries", []):
        anchor(boundary.get("file"), boundary.get("line"), boundary.get("line"),
               "boundary:" + boundary.get("id", ""), 2, center=boundary.get("line"))
        for effect in boundary.get("K_b", {}).get("static_obligations", []):
            anchor(effect.get("file"), effect.get("line"), effect.get("line"), "static_obligation", 2, center=effect.get("line"))
    for hid, binding in state.get("scope", {}).get("bindings", {}).items():
        for effect in binding.get("effect_evidence", []):
            anchor(effect.get("file"), effect.get("line"), effect.get("line"), "static_effect:" + hid, 2, center=effect.get("line"))
    if not minimal:
        from .front import imported_symbols, export_names
        initial = list(anchors)
        helpers, dependencies = 0, set()
        for item in initial:
            name, line = item["file"], item["center"] or item["start"]
            entry = facts.get(name, {})
            if entry and entry.get("source_sha256") != sources[name]["sha256"]:
                kind = "source_version_conflict" if entry.get("source_sha256") else "static_index_unversioned"
                _gap(audit, kind, file=name, reason="static_index_version")
                continue
            functions = entry.get("functions", [])
            owners = [f for f in functions if f.get("line", 0) <= line <= f.get("end_line", -1)]
            owner = min(owners, key=lambda f: f["end_line"] - f["line"]) if owners else None
            if owner:
                anchor(name, owner["line"], min(owner["line"] + 2, owner["end_line"]), "function_signature", 2)
            tokens = set(re.findall(r"[$A-Za-z_][$\w]*", sources[name]["lines"][line - 1]))
            for node in entry.get("declarations", []):
                if node.get("name") in tokens and node.get("line", line) < line:
                    anchor(name, node["line"], node["end_line"], "local_definition", 2)
            for node in functions:
                if node.get("name") in tokens and helpers < config["max_direct_helpers"]:
                    anchor(name, node["line"], node["end_line"], "direct_helper", 3)
                    helpers += 1
        seed_names = {item["file"] for item in initial}
        known_names = set(facts) | seed_names | set(payload["edit_contract"]["allowed_change"])
        for edge in state.get("scope", {}).get("edges", []):
            known_names.update(v for v in (edge.get("from"), edge.get("to")) if isinstance(v, str))
        for name in dict.fromkeys(item["file"] for item in initial if item["center"] is not None):
            positions = [item["center"] for item in initial if item["file"] == name and item["center"] is not None]
            text = "".join(sources[name]["lines"][line - 1] for line in positions)
            tokens = set(re.findall(r"[$A-Za-z_][$\w]*", _masked(text)))
            for binding in imported_symbols(sources[name]["text"]):
                if binding["local"].split(".")[0] not in tokens:
                    continue
                target = _resolve(name, binding["request"], known_names)
                if target is None:
                    _gap(audit, "unresolved_dependency", file=name, request=binding["request"], symbol=binding["local"])
                    continue
                if target not in seed_names | dependencies and len(dependencies) >= config["max_dependency_files"]:
                    _gap(audit, "not_read", file=target, reason="dependency_file_limit")
                    continue
                value = source(target)
                if value is None:
                    continue
                if target not in seed_names:
                    dependencies.add(target)
                names = {binding["imported"]} | set(export_names({"masked": _masked(value["text"])}).get(binding["imported"], []))
                entry = facts.get(target, {})
                definitions = [node for node in entry.get("functions", []) if node.get("name") in names]
                if entry.get("source_sha256") != value["sha256"]:
                    definitions = []
                for node in definitions[:config["max_direct_helpers"]]:
                    anchor(target, node["line"], node["line"], "import_definition", 2, center=node["line"])
                    anchor(target, node["line"], node["end_line"], "import_definition_body", 3)
                if not definitions:
                    _gap(audit, "definition_unresolved", file=target, symbol=binding["imported"])
                    if value["lines"]:
                        anchor(target, 1, min(len(value["lines"]), 2 * radius + 1), "import_context", 3)
        for edge in state.get("scope", {}).get("edges", []):
            target = edge.get("to") if edge.get("from") in seed_names else edge.get("from") if edge.get("to") in seed_names else None
            if not target or target in seed_names or target in dependencies:
                continue
            if len(dependencies) >= config["max_dependency_files"]:
                _gap(audit, "not_read", file=target, reason="dependency_file_limit")
                continue
            dependencies.add(target)
            value = source(target)
            if value and value["lines"]:
                anchor(target, 1, min(len(value["lines"]), 2 * radius + 1), "interface_dependency", 3)
                symbols = {h.get("symbol", "").split(".")[-1] for h in payload["hypotheses"]["items"] if h.get("file") in seed_names}
                entry = facts.get(target, {})
                if entry.get("source_sha256") == value["sha256"]:
                    calls = [call for call in entry.get("calls", []) if call.get("callee", "").split(".")[-1] in symbols]
                    for call in calls[:config["max_direct_helpers"]]:
                        anchor(target, call["line"], call["line"], "direct_caller", 3, center=call["line"])
        for file in state.get("scope", {}).get("context", []):
            for window in file.get("windows", []):
                anchor(file.get("file"), window.get("start_line"), window.get("end_line"), "legacy_scope_location", 4)
    if not anchors:
        for name in payload["edit_contract"].get("ranked_files", [])[:config["fallback_max_files"]]:
            value = source(name)
            if value and value["lines"]:
                anchor(name, 1, min(len(value["lines"]), 2 * radius + 1), "ranked_file_fallback", 0, center=1)
    accepted = []
    for item in sorted(anchors, key=lambda a: a["priority"]):
        value = sources[item["file"]]
        start, end = item["start"], item["end"]
        if item["center"] is not None:
            start, end = max(1, start - radius), min(len(value["lines"]), end + radius)
            while True:
                text = "".join(value["lines"][start - 1:end])
                if len(text.encode()) > config["max_window_utf8_bytes"]:
                    start = end = item["center"]
                    break
                if _match_count(value["text"], text) == 1 or (start == 1 and end == len(value["lines"])):
                    break
                low, high = max(1, start - radius), min(len(value["lines"]), end + radius)
                if len("".join(value["lines"][low - 1:high]).encode()) > config["max_window_utf8_bytes"]:
                    break
                start, end = low, high
        need = {**item, "start": start, "end": end}
        if any(len(line.encode()) > config["max_window_utf8_bytes"] for line in value["lines"][start - 1:end]):
            _gap(audit, "budget_omitted", file=item["file"], start_line=start, end_line=end,
                 origin=item["purpose"], reason="single_line_exceeds_window_budget")
            continue
        windows = _s2_windows(sources, [*accepted, need], config["max_window_utf8_bytes"])
        if sum(len(w["text"].encode()) for w in windows) > config["max_source_total_utf8_bytes"]:
            _gap(audit, "budget_omitted", file=item["file"], start_line=start, end_line=end,
                 origin=item["purpose"], reason="source_total_byte_budget")
            continue
        accepted.append(need)
        if minimal and any(w["file"] in payload["edit_contract"]["allowed_change"]
                           and _match_count(sources[w["file"]]["text"], w["text"]) == 1 for w in windows):
            break
    windows = _s2_windows(sources, accepted, config["max_window_utf8_bytes"])
    for window in windows:
        source = sources[window["file"]]
        start_byte = len("".join(source["lines"][:window["start_line"] - 1]).encode())
        window.update(byte_range=[start_byte, start_byte + len(window["text"].encode())],
                      whole_window_match_count=_match_count(source["text"], window["text"]),
                      origin="verified_base_context_not_new_agent_evidence")
    audit["source_needs"] = anchors
    for item in anchors:
        if not source_range_covered(windows, item["file"], item["start"], item["end"]):
            _gap(audit, "source_not_fully_expanded", file=item["file"], start_line=item["start"], end_line=item["end"], origin=item["purpose"])
    return windows


def _evidence(state, payload, audit, *, minimal):

    evidence, tool_ids = payload["evidence"], {}
    relevant_files = {w["file"] for w in payload["source_windows"]} | {h.get("file") for h in payload["hypotheses"]["items"]}
    active = {h["id"] for h in payload["hypotheses"]["items"] if any(w["file"] == h.get("file")
        and type(h.get("line")) is int and w["start_line"] <= h["line"] <= w["end_line"] for w in payload["source_windows"])}
    cited = {eid for r in state.get("reports", {}).values() for c in r.get("report", {}).get("claims", []) for eid in c.get("evidence_ids", [])}
    scene = None

    def add(identifier, item, origin):
        evidence[identifier] = item
        audit["evidence_origins"][identifier] = origin
        return identifier

    def witness(identifier, record, origin):
        base = _select(record, ("id", "source_sha256", "symbol_exists", "instrumented_line", "static_reachable", "coverage", "static_certificate"))
        owner = next((h.get("file") for h in payload["hypotheses"]["items"] if h["id"] == record.get("id")), None)
        version = audit["source_versions"].get(owner)
        if version and record.get("source_sha256") not in {None, "", version}:
            base["base_version_status"] = "conflict"
            _gap(audit, "witness_source_version_conflict", hypothesis_id=record.get("id"), origin=origin)
        layers = record.get("tiers", [])
        if minimal and active and record.get("id") not in active:
            _gap(audit, "evidence_not_expanded", hypothesis_id=record.get("id"), origin=origin,
                 reason="minimal_source_direction_not_expanded")
            return add(identifier, {"kind": "unexpanded_witness", "hypothesis_id": record.get("id"),
                "content_available": False, "reason": "minimal_source_direction_not_expanded"}, origin)
        refs = []
        for ordinal, layer in enumerate(layers):
            key = f"{identifier}:tier:{ordinal}"
            refs.append(add(key, {"kind": "runtime_observation", "hypothesis_id": record.get("id"),
                "execution_identity": identifier, **_select(layer, RUNTIME_FIELDS)}, origin + f"/tiers/{ordinal}"))
        fields = _select(record, RUNTIME_FIELDS)
        aliases = [ref for ref in refs if encode(_select(evidence[ref], fields)) == encode(fields)]
        base.update(kind="witness_snapshot", tier_refs=refs)
        if aliases:
            base["selected_tier_ref"] = aliases[0]
            audit["deduplicated_witness_wrappers"] += 1
            audit["deduplicated_witness_utf8_bytes"] += text_size(fields)["utf8_bytes"]
        else:
            base.update(fields)
        if record.get("artifact"):
            base["artifact_visibility"] = "not_attached_to_s5"
        return add(identifier, base, origin)

    for agent, ordinal, observation in _observations(state):
        identity = observation.get("evidence_id")
        if not isinstance(identity, str) or identity in tool_ids:
            _gap(audit, "invalid_evidence_identity", agent=agent, ordinal=ordinal)
            continue
        tool, raw = observation.get("tool", {}), observation.get("result", {})
        if not isinstance(tool, dict) or not isinstance(raw, dict):
            _gap(audit, "raw_evidence_unavailable", evidence_id=identity)
            continue
        name = tool.get("name", "")
        identifier = "tool:" + identity
        item = {"kind": "tool_observation", "agent": agent, "evidence_id": identity, "tool_name": name,
                **_select(raw, ("ok", "error", "reason", "provenance", "base_commit", "source_id", "resolved_path", "source_kind"))}
        item["request"] = _select(tool, ("source_id", "start_line", "end_line", "query", "actions"))
        origin = f"reports/{agent}/observations/{ordinal}/result"
        if name == "read_source":
            item.update(_select(raw, ("file", "sha256", "total_lines", "truncated")))
            item["source_visibility"] = source_visibility(tool, raw)
            verified = audit["verified_reads"].get(identity)
            item["source_refs"] = [w["id"] for w in payload["source_windows"] if verified and w["file"] == verified["file"]
                and w["source_sha256"] == verified["source_sha256"] and w["start_line"] <= verified["end_line"] and verified["start_line"] <= w["end_line"]]
            item["original_read_range"] = verified
            item["content_status"] = "verified_base_windows" if item["source_refs"] else "not_available_in_s5"
            ranges = _merge([(w["start_line"], w["end_line"]) for w in payload["source_windows"] if w["id"] in item["source_refs"]])
            item["content_complete"] = bool(verified and any(a <= verified["start_line"] and verified["end_line"] <= b for a, b in ranges))
            if raw.get("ok") and not item["source_refs"]:
                _gap(audit, "source_evidence_omitted", evidence_id=identity, file=raw.get("file"))
            elif raw.get("ok") and not item["content_complete"]:
                _gap(audit, "source_evidence_partial", evidence_id=identity, file=raw.get("file"))
        elif name in {"reproduce", "interact"}:
            if name == "reproduce" and raw.get("error") not in {"draft_required", "browser_action_limit_8"}:
                scene = identifier
            item.update(scene_ref=scene, runtime_session_identity="not_provided", failed_action="unknown")
            if name == "reproduce" and tool.get("draft") is not None:
                item["scenario"] = deepcopy(tool["draft"])
            for field in ("build", "node", "browser"):
                value = raw.get(field)
                if not isinstance(value, dict):
                    continue
                item[field] = _select(value, ("ok", "reason", "returncode", "entry_exists", "state", "completed_actions",
                    "query", "dom", "actual_scene", "stderr"))
                if minimal and active and isinstance(item[field].get("state"), dict):
                    runtime = item[field]["state"]
                    events = runtime.get("events", [])
                    runtime["events"] = [e for e in events if str(e.get("id")) in active]
                    if len(events) != len(runtime["events"]):
                        _gap(audit, "evidence_not_expanded", evidence_id=identity, field=field + ".state.events",
                             omitted=len(events) - len(runtime["events"]), reason="minimal_source_direction_not_expanded")
                if isinstance(value.get("process"), dict):
                    item[field]["process"] = _select(value["process"], ("returncode", "command", "reason", "timeout", "timed_out"))
                if field == "browser" and "html" in value:
                    _gap(audit, "content_omitted", evidence_id=identity, field="browser.html", reason="default_full_html")
            item.update(_select(raw, ("build_stdout", "build_stderr", "node_stderr")))
            item["witness_refs"] = [witness(f"runtime:{identity}:{i}", w, origin + f"/witnesses/{i}")
                                    for i, w in enumerate(raw.get("witnesses", []))]
            if raw.get("screenshot"):
                item["runtime_image_attached_to_s5"] = False
        elif name == "run_tests":
            item.update(_select(raw, ("status", "runner", "selected_test", "summary", "output_tail", "seed")))
            item["process"] = _select(raw.get("process", {}), ("returncode", "command", "reason", "timeout", "timed_out"))
        elif name in {"search_source", "list_sources"}:
            field = "matches" if name == "search_source" else "files"
            rows = raw.get(field, [])
            kept = [r for r in rows if r.get("file") in relevant_files]
            item.update(_select(raw, ("total", "total_matches", "next_offset", "truncated")))
            item[field] = deepcopy(kept)
            item["omitted_results"] = len(rows) - len(kept)
            item["match_text_is_editable_source"] = False
        elif name in {"dependencies", "symbols", "project_info"}:
            file = raw.get("file", raw.get("resolved_path"))
            if file not in relevant_files and identity not in cited and raw.get("ok") is True:
                _gap(audit, "content_omitted", evidence_id=identity, reason="unrelated_static_tool_record")
                continue
            item.update(_select(raw, ("file", "neighbors", "neighbor_sources", "edges", "unresolved", "static_only",
                                      "probes", "declarations", "members", "scripts", "test_runner")))
        else:
            _gap(audit, "content_omitted", evidence_id=identity, reason="unknown_tool_fields")
        tool_ids[identity] = add(identifier, item, origin)
    final_refs = {}
    for index, record in enumerate(state.get("witnesses", [])):
        ref = witness(f"final:{index}", record, f"witnesses/{index}")
        final_refs.setdefault(record.get("id"), []).append((record, ref))
    for original, h in zip(state["root"]["hypotheses"], payload["hypotheses"]["items"]):
        for record in original.get("certificate", {}).get("witnesses", []):
            matches = [ref for raw, ref in final_refs.get(h["id"], []) if encode(raw) == encode(record)]
            ref = matches[0] if matches else witness(f"certificate:{h['id']}:{len(h['certificate']['witness_refs'])}", record,
                                                     f"root/hypotheses/{h['id']}/certificate/witnesses")
            h["certificate"]["witness_refs"].append(ref)
        h["evidence_refs"] = [key for key, e in evidence.items() if e.get("hypothesis_id", e.get("id")) == h["id"]]
        h["source_refs"] = [w["id"] for w in payload["source_windows"] if w["file"] == h.get("file")
                            and type(h.get("line")) is int and w["start_line"] <= h["line"] <= w["end_line"]]
    if not minimal:
        for index, boundary in enumerate(state.get("front_context", {}).get("selected_boundaries", [])):
            item = _select(boundary, ("id", "file", "line", "end_line", "mechanism", "repair_interface", "expressivity"))
            item.update(kind="static_boundary", constraint_ids=deepcopy(boundary.get("K_b", {}).get("constraint_ids", [])),
                upstream_interpretation={"selection_reason": boundary.get("selection_reason", ""),
                                         "preserve": deepcopy(boundary.get("K_b", {}).get("declared_preserve", []))})
            item["static_obligations"] = [_select(effect, ("file", "line", "kind", "property", "reads", "writes", "static_only", "outside_boundary"))
                                          for effect in boundary.get("K_b", {}).get("static_obligations", [])]
            add(f"boundary:{index}", item, f"front_context/selected_boundaries/{index}")
        for hid, binding in state.get("scope", {}).get("bindings", {}).items():
            item = _select(binding, ("symbol_exists", "readable_symbols", "readable_symbols_basis", "interface_complete",
                                    "requested_interface_covered", "interface_basis"))
            item.update(kind="static_binding", hypothesis_id=hid, basis="legacy_scope_not_s5_windows",
                        downstream_summary=_select(binding.get("downstream_summary", {}), ("status", "reason", "unknown_reasons")))
            item["effect_locations"] = [{**_select(effect, ("file", "line", "kind", "static_only", "target_binding_verified")),
                "source_refs": [w["id"] for w in payload["source_windows"] if w["file"] == effect.get("file")
                    and type(effect.get("line")) is int and w["start_line"] <= effect["line"] <= w["end_line"]]}
                for effect in binding.get("effect_evidence", [])]
            add("binding:" + hid, item, "scope/bindings/" + hid)
        for message in state.get("front_context", {}).get("scope_plan", {}).get("unresolved", []):
            _gap(audit, "localization_unresolved", origin="S0_scope", text=message)
    for agent in ("code", "browser"):
        raw = state.get("reports", {}).get(agent, {})
        report = raw.get("report", {})
        completed = raw.get("stop_reason") == "completed"
        view = {**_select(raw, ("stop_reason", "calls", "call_limit", "error", "error_type")),
            "model_report_available": completed, "report_origin": "model_interpretation" if completed else "controller_status",
            "summary": report.get("summary", ""), "limitations": deepcopy(report.get("limitations", [])), "claims": [],
            "evidence_refs": [ref for key, ref in tool_ids.items() if key.startswith(agent + ".")]}
        for index, claim in enumerate(report.get("claims", []) if completed else []):
            item = _select(claim, ("hypothesis_id", "interpretation", "repair_suggestion", "citations_valid", "evidence_kind"))
            item["original_evidence_ids"] = deepcopy(claim.get("evidence_ids", []))
            item["evidence_refs"] = [tool_ids[eid] for eid in claim.get("evidence_ids", []) if eid in tool_ids]
            item["support_status"] = "model_interpretation_not_verified"
            for eid in claim.get("evidence_ids", []):
                if eid not in tool_ids:
                    _gap(audit, "invalid_reference", agent=agent, claim=index, evidence_id=eid)
            view["claims"].append(item)
        if not completed and raw.get("stop_reason") != "disabled_by_ablation":
            _gap(audit, "agent_final_report_unavailable", agent=agent, stop_reason=raw.get("stop_reason", "missing"))
        agent_audit = state.get("agent_audits", {}).get(agent, {})
        if isinstance(agent_audit, dict):
            feedback = agent_audit.get("feedback", [])
            feedback = feedback if isinstance(feedback, list) else []
            failures = [a.get("failure") for turn in feedback if isinstance(turn, dict) and isinstance(turn.get("attempts"), list)
                        for a in turn["attempts"] if isinstance(a, dict) and a.get("failure")]
            if failures:
                view["feedback_failures"] = deepcopy(failures)
        else:
            _gap(audit, "audit_unavailable", agent=agent, reason="invalid_optional_audit")
        payload["agents"][agent] = view


def _deduplicate_metadata(payload, audit):

    groups = {}
    for h in payload["hypotheses"]["items"]:
        if h.get("readable_symbols"):
            groups.setdefault((h.get("file"), encode(h["readable_symbols"])), []).append((h, "readable_symbols"))
    for item in list(payload["evidence"].values()):
        if item["kind"] == "static_boundary":
            interface = item.get("repair_interface", {})
            if interface.get("z_b"):
                groups.setdefault((item.get("file"), encode(interface["z_b"])), []).append((interface, "z_b"))
        elif item["kind"] == "static_binding" and item.get("readable_symbols"):
            name = next((h.get("file") for h in payload["hypotheses"]["items"] if h["id"] == item["hypothesis_id"]), None)
            groups.setdefault((name, encode(item["readable_symbols"])), []).append((item, "readable_symbols"))
    audit["deduplicated_static_text_utf8_bytes"] = 0
    for (name, body), entries in groups.items():
        if len(entries) < 2 or len(body.encode()) < 256:
            continue
        identifier = "symbols:" + str(len(payload["evidence"]))
        value = entries[0][0][entries[0][1]]
        item = {"kind": "static_symbol_list", "file": name, "symbols": deepcopy(value),
                "meaning": "shared_text_only_each_referring_record_keeps_its_own_evidence_scope"}
        if len(entries) * text_size(value)["utf8_bytes"] <= text_size(item)["utf8_bytes"] + len(entries) * (len(identifier) + 32):
            continue
        payload["evidence"][identifier] = item
        audit["evidence_origins"][identifier] = "same_file_static_read_candidates"
        audit["deduplicated_static_text_utf8_bytes"] += (len(entries) - 1) * text_size(value)["utf8_bytes"]
        for container, key in entries:
            del container[key]
            container[key + "_ref"] = identifier


def _compact_gaps(gaps):

    result, groups = [], {}
    for gap in gaps:
        detail = gap.get("detail")
        if (gap["kind"] != "upstream_scope_gap" or not isinstance(detail, dict) or "request" not in detail
                or set(detail) - {"from", "request", "reason", "kind"}):
            result.append(deepcopy(gap))
            continue
        base = _select(detail, ("from", "reason", "kind"))
        key = encode(base)
        if key not in groups:
            groups[key] = {"kind": "upstream_scope_gaps", "detail": base, "requests": []}
            result.append(groups[key])
        if detail["request"] not in groups[key]["requests"]:
            groups[key]["requests"].append(detail["request"])
    return result


def reference_errors(payload):

    sources = {w["id"] for w in payload["source_windows"]}
    evidence = set(payload["evidence"])
    errors = []

    def walk(value, pointer=""):
        if isinstance(value, dict):
            for key, child in value.items():
                location = pointer + "/" + key
                if key == "feedback_content_ref":
                    errors.append({"pointer": location, "reason": "agent_history_reference"})
                if key in {"source_refs", "evidence_refs", "witness_refs", "tier_refs"}:
                    valid = sources if key == "source_refs" else evidence
                    errors.extend({"pointer": location, "id": ref, "reason": "missing_inline_content"} for ref in child if ref not in valid)
                elif key in {"selected_tier_ref", "scene_ref", "readable_symbols_ref", "z_b_ref"} and child is not None and child not in evidence:
                    errors.append({"pointer": location, "id": child, "reason": "missing_inline_content"})
                walk(child, location)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, pointer + "/" + str(index))
    walk(payload)
    return errors


def s5_context_metrics(payload, request_context=None, images=()):

    windows = payload["source_windows"]
    lines = [(w["file"], w["source_sha256"], line) for w in windows for line in range(w["start_line"], w["end_line"] + 1)]
    coverage = [{"hypothesis_id": h["id"], "file": h.get("file"), "line": h.get("line"),
                 "target_covered": bool(h["source_refs"]),
                 "unique_edit_window": any(w["id"] in h["source_refs"] and w["whole_window_match_count"] == 1
                     and w["file"] in payload["edit_contract"]["allowed_change"] for w in windows)} for h in payload["hypotheses"]["items"]]
    return {"payload": text_size(payload), "sections": {key: text_size(value) for key, value in payload.items()},
            "request_text": request_size(request_context or {"messages": []},
                [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}, *images]),
            "source_characters": sum(len(w["text"]) for w in windows),
            "source_utf8_bytes": sum(len(w["text"].encode()) for w in windows),
            "source_windows": len(windows), "duplicate_source_lines": len(lines) - len(set(lines)),
            "evidence": text_size(payload["evidence"]), "coverage": coverage,
            "reference_errors": reference_errors(payload), "omissions": len(payload["gaps"]), "actual_input_tokens": None}


def _build(repo, state, config, request_context, *, minimal):

    audit = {"protocol": PROTOCOL, "mode": "minimal" if minimal else "normal", "status": "building",
             "config": config, "gaps": [], "source_versions": {}, "tool_source_visibility": {}, "verified_reads": {},
             "evidence_origins": {}, "deduplicated_constraints": 0, "deduplicated_witness_wrappers": 0,
             "deduplicated_witness_utf8_bytes": 0,
             "automatic_reads_are_agent_tool_calls": False, "failure": None}

    def fail(reason):
        audit.update(status="unavailable", failure={"reason": reason})
        return None, audit

    payload, error = _core(state, audit)
    if error:
        return fail(error)
    for gap in state.get("upstream_gaps", []):
        _gap(audit, **gap)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True)
    if head.returncode or head.stdout.decode().strip() != payload["task"]["base_commit"]:
        return fail("base_identity_unavailable")
    if any(not _safe_path(repo, name) for name in payload["edit_contract"]["allowed_change"]):
        return fail("invalid_edit_permissions")
    if not minimal:
        boundaries = state.get("front_context", {}).get("selected_boundaries", [])
        if not isinstance(boundaries, list) or any(not isinstance(b, dict) or not isinstance(b.get("K_b", {}), dict) for b in boundaries):
            return fail("optional_boundary_projection_invalid")
    if minimal:
        state = {**state, "front_context": {"selected_boundaries": []}, "s0_facts": {},
                 "scope": _select(state.get("scope", {}), ("baseline_topk",))}
        _gap(audit, "minimal_projection", reason="normal_projection_unavailable", omissions="optional_static_expansion_and_secondary_source_windows")
    payload["source_windows"] = _sources(repo, state, payload, audit, config, minimal=minimal)
    editable = any(w["file"] in payload["edit_contract"]["allowed_change"] and w["whole_window_match_count"] == 1 for w in payload["source_windows"])
    if not editable and not (payload["edit_contract"]["new_files"] and payload["source_windows"]):
        return fail("minimum_editable_source_unavailable")
    _evidence(state, payload, audit, minimal=minimal)
    _deduplicate_metadata(payload, audit)
    scope = state.get("scope", {})
    for key in ("unresolved_dependencies",):
        for item in scope.get(key, []):
            _gap(audit, "upstream_scope_gap", detail=deepcopy(item))
    payload["gaps"] = _compact_gaps(audit["gaps"])
    audit["metrics"] = s5_context_metrics(payload, request_context, state.get("images", []))
    if audit["metrics"]["reference_errors"]:
        return fail("internal_reference_unavailable")
    for metric, key in (("evidence", "max_evidence_utf8_bytes"), ("payload", "max_payload_utf8_bytes"),
                        ("request_text", "max_request_text_utf8_bytes")):
        if audit["metrics"][metric]["utf8_bytes"] > config[key]:
            return fail(key)
    audit["status"] = "degraded" if minimal else "ready_with_gaps" if audit["gaps"] else "ready"
    audit["payload_sha256"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
    return payload, audit


def build_synthesis_payload(repo, *, budget=None, request_context=None, **state):

    return _build(Path(repo).resolve(), state, s5_context_config(budget), request_context, minimal=False)


def build_minimal_synthesis_payload(repo, *, budget=None, request_context=None, **state):

    return _build(Path(repo).resolve(), state, s5_context_config(budget), request_context, minimal=True)


def construct_synthesis_payload(repo, **arguments):

    attempts = []
    for builder in (build_synthesis_payload, build_minimal_synthesis_payload):
        payload, audit = builder(repo, **arguments)
        attempts.append(audit)
        if payload is not None:
            return payload, {"protocol": PROTOCOL, "status": audit["status"], "mode": audit["mode"],
                "attempts": attempts, "metrics": audit["metrics"], "payload_sha256": audit["payload_sha256"]}
    return None, {"protocol": PROTOCOL, "status": "unavailable", "mode": "unavailable", "attempts": attempts,
                  "metrics": {"actual_input_tokens": None}, "failure": attempts[-1]["failure"]}
