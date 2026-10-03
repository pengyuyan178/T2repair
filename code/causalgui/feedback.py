from copy import deepcopy
import hashlib
import json


PROTOCOL = "tool_feedback_v1"
DEFAULT_BUDGET = {"max_observation_utf8_bytes": 131072,
                  "max_feedback_utf8_bytes": 524288,
                  "max_request_text_utf8_bytes": 2097152}
COMMON = {"ok", "error", "reason", "source_id", "resolved_path", "source_kind", "provenance", "base_commit"}
FIELDS = {
    "read_source": {"file", "start_line", "end_line", "text", "truncated", "sha256", "total_lines"},
    "search_source": {"matches", "total_matches", "truncated", "next_offset"},
    "list_sources": {"files", "total", "next_offset"},
    "dependencies": {"file", "neighbors", "neighbor_sources", "edges", "unresolved", "static_only"},
    "symbols": {"probes", "declarations", "members", "process"},
    "project_info": {"scripts", "test_runner", "test_sources"},
    "run_tests": {"status", "runner", "selected_test", "process", "summary", "output_tail", "seed"},
    "reproduce": {"build", "node", "browser", "build_stdout", "build_stderr", "node_stderr", "screenshot", "witnesses"},
    "interact": {"browser", "screenshot", "witnesses"},
    "": set(),
}
RUNTIME_FIELDS = {"tier", "execution_ok", "reached", "values", "scenario_complete", "target_loaded",
                  "artifact", "reason", "errors", "evidence"}


def encode(value):

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def size(value):

    text = value if isinstance(value, str) else encode(value)
    return {"chars": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def digest(value):

    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def feedback_config(overrides=None):

    overrides = overrides or {}
    assert set(overrides) <= {"protocol", "code", "browser"}
    assert overrides.get("protocol", PROTOCOL) == PROTOCOL
    result = {"protocol": PROTOCOL}
    for agent in ("code", "browser"):
        values = overrides.get(agent, {})
        assert set(values) <= DEFAULT_BUDGET.keys()
        result[agent] = {**DEFAULT_BUDGET, **values}
        assert all(type(v) is int and v > 0 for v in result[agent].values())
    return result


def source_visibility(tool, result):

    text = result.get("text")
    start, end = result.get("start_line"), result.get("end_line")
    if not isinstance(text, str) or type(start) is not int or type(end) is not int or start < 1 or end < start:
        return {"status": "unknown", "reason": "source_text_or_range_unavailable"}
    lines = text.splitlines(keepends=True)
    actual_end = start + len(lines) - 1 if lines else None
    within_cap = len(text) < 24000 or (len(text) == 24000 and result.get("truncated") is False)
    consistent = bool(lines) and actual_end <= end
    if within_cap and type(result.get("total_lines")) is int and end < result["total_lines"] and text and text[-1] not in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029":
        consistent = False
    whole = consistent and within_cap and actual_end == end
    terminated = bool(text) and text[-1] in "\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    last = "complete" if whole or terminated else "unknown"
    if consistent and actual_end < end and len(text) == 24000 and not terminated and not text.endswith("\r"):
        last = "partial"
    cut = False if within_cap else True if consistent and actual_end < end else None
    return {"status": "complete" if whole else "partial" if cut is True else "unknown",
            "requested_range": [tool.get("start_line"), tool.get("end_line")],
            "tool_line_window": [start, end], "raw_text_range": [start, actual_end],
            "visible_text_range": [start, actual_end], "last_line": last,
            "complete_through_line": actual_end if last == "complete" else actual_end - 1 if actual_end else None,
            "tool_truncation": {"reported": result.get("truncated"), "character_cut": cut,
                                "character_cap": 24000, "unit": "python_characters",
                                "more_file_lines": end < result["total_lines"] if type(result.get("total_lines")) is int else None},
            "projection_omission": "none"}


def _pointer(value, pointer):

    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list) and token.isdecimal() and int(token) < len(value):
            value = value[int(token)]
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            return False, None
    return True, value


def _child(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _leaves(value, path=""):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _leaves(child, _child(path, key))
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from _leaves(child, _child(path, index))
    else:
        yield path, value


def resolve_reference(reference, index, current=None):

    if reference.get("agent") != index["agent"]:
        return False, None
    key = (reference.get("message"), reference.get("part"))
    document = current if key == (index["next_message"], 0) and current is not None else index["documents"].get(key)
    if document is None:
        return False, None
    found, value = _pointer(document, reference.get("pointer", ""))
    if not found:
        return False, None
    if "text_slice" in reference:
        bounds = reference["text_slice"]
        if not isinstance(value, str) or len(bounds) != 2 or not 0 <= bounds[0] <= bounds[1] <= len(value):
            return False, None
        value = value[bounds[0]:bounds[1]]
    return digest(value) == reference.get("sha256"), value


def _reference(index, message, part, pointer, value, **extra):
    return {"agent": index["agent"], "message": message, "part": part, "pointer": pointer,
            "sha256": digest(value), **extra}


def visible_context_index(agent, history):

    index = {"agent": agent, "documents": {}, "sources": [], "next_message": len(history)}
    for message, row in enumerate(history):
        parts = row["content"] if isinstance(row["content"], list) else [{"type": "text", "text": row["content"]}]
        for part, content in enumerate(parts):
            if content.get("type") != "text" or not content["text"].lstrip().startswith("{"):
                continue
            document = json.loads(content["text"])
            index["documents"][(message, part if isinstance(row["content"], list) else None)] = document
    for (message, part), document in index["documents"].items():
        if not isinstance(document, dict):
            continue
        for number, window in enumerate(document.get("source_windows", [])):
            text = window.get("text")
            if not isinstance(text, str) or not window.get("source_sha256"):
                continue
            start, end = window.get("start_line"), window.get("end_line")
            if type(start) is int and type(end) is int and start > 0 and len(text.splitlines(keepends=True)) == end - start + 1:
                index["sources"].append({"file": window["file"], "sha256": window["source_sha256"],
                    "start": start, "end": end, "text": text, "origin": "initial_context",
                    "ref": _reference(index, message, part, f"/source_windows/{number}/text", text)})
        for number, observation in enumerate(document.get("tool_observations", [])):
            result, tool = observation.get("result", {}), observation.get("tool", {})
            name = tool.get("name", observation.get("tool_name"))
            if name != "read_source" or not isinstance(result.get("text"), str):
                continue
            coverage = source_visibility(tool, result)
            if coverage["status"] == "complete" and result.get("sha256") and result.get("file"):
                index["sources"].append({"file": result["file"], "sha256": result["sha256"],
                    "start": result["start_line"], "end": result["end_line"], "text": result["text"],
                    "origin": "tool_observation", "ref": _reference(index, message, part,
                        f"/tool_observations/{number}/result/text", result["text"])})
    return index


def request_size(context, feedback):

    messages = deepcopy(context["messages"])
    if feedback is not None:
        messages.append({"role": "user", "content": deepcopy(feedback)})
    if context.get("next_turn") is not None:
        messages.append(deepcopy(context["next_turn"]))
    image_count = encoded_bytes = binary_bytes = 0
    for message in messages:
        if not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            if part.get("type") != "image_url":
                continue
            image_count += 1
            url = part["image_url"]["url"]
            encoded_bytes += len(url.encode("utf-8"))
            if url.startswith("data:") and ";base64," in url and binary_bytes is not None:
                data = url.split(",", 1)[1]
                binary_bytes += len(data) * 3 // 4 - len(data) + len(data.rstrip("="))
            elif not url.startswith("data:") or ";base64," not in url:
                binary_bytes = None
            part["image_url"]["url"] = ""
    body = {"messages": messages}
    if context.get("response_format") is not None:
        body["response_format"] = context["response_format"]
    return {**size(body), "image_count": image_count, "image_url_utf8_bytes": encoded_bytes,
            "image_binary_bytes": binary_bytes, "tokens": None,
            "measurement": "canonical_json_messages_and_response_format_without_image_urls"}


def _artifact_paths(result):

    paths = ["/screenshot", "/artifact"]
    for prefix in ("", "/node", "/browser"):
        paths += [prefix + "/process/" + key for key in ("stdout", "stderr")]
    paths += ["/build/stdout", "/build/stderr", "/node/artifact", "/browser/screenshot", "/browser/artifact"]
    for i, witness in enumerate(result.get("witnesses", [])):
        paths.append(f"/witnesses/{i}/artifact")
        paths.extend(f"/witnesses/{i}/tiers/{j}/artifact" for j in range(len(witness.get("tiers", []))))
    return paths


def _remove(value, path):
    parent, _, key = path.rpartition("/")
    found, container = _pointer(value, parent)
    if found and isinstance(container, dict) and key in container:
        del container[key]
        return True
    return False


def _source_reference(index, result, coverage):
    if coverage["status"] != "complete" or not result.get("sha256"):
        return None
    for source in index["sources"]:
        if source["file"] != result.get("file") or source["sha256"] != result["sha256"]:
            continue
        if not source["start"] <= result["start_line"] <= result["end_line"] <= source["end"]:
            continue
        lines = source["text"].splitlines(keepends=True)
        low = sum(map(len, lines[:result["start_line"] - source["start"]]))
        high = sum(map(len, lines[:result["end_line"] - source["start"] + 1]))
        if source["text"][low:high] == result["text"]:
            ref = {**source["ref"], "text_slice": [low, high], "sha256": digest(result["text"]), "origin": source["origin"]}
            if resolve_reference(ref, index)[0]:
                return ref
    return None


def _request_reference(index, tool):
    for (message, part), document in reversed(list(index["documents"].items())):
        if not isinstance(document, dict) or document.get("action") != "tools":
            continue
        for number, candidate in enumerate(document.get("tools", [])):
            if digest(candidate) == digest(tool):
                return _reference(index, message, part, f"/tools/{number}", tool)
        break
    return None


def build_agent_feedback(agent, observations, visible_context_index, remaining_model_calls, budget=None,
                         *, request_context, image_parts=(), image_bindings=(), mode="normal"):

    index = visible_context_index
    config = {**DEFAULT_BUDGET, **(budget or {})}
    audit = {"protocol": PROTOCOL, "agent": agent, "mode": mode, "status": "building",
             "raw_sha256": digest(observations), "budget": config, "field_mapping": [], "omissions": [],
             "references": [], "pending_validation": [], "artifact_fields": [], "observations": [], "failure": None}

    def fail(reason, **details):
        audit.update(status="failed", failure={"reason": reason, **details})
        return None, audit

    if agent not in {"code", "browser"} or index.get("agent") != agent or mode not in {"normal", "emergency"}:
        return fail("invalid_feedback_context")
    if type(remaining_model_calls) is not int or remaining_model_calls < 1:
        return fail("no_remaining_summary_call")
    if set(config) != DEFAULT_BUDGET.keys() or any(type(v) is not int or v <= 0 for v in config.values()):
        return fail("invalid_feedback_budget")
    if len(image_parts) != len(image_bindings):
        return fail("image_binding_count_mismatch")
    identifiers = [o.get("evidence_id") for o in observations]
    if any(not isinstance(i, str) or not i.startswith(agent + ".") for i in identifiers) or len(set(identifiers)) != len(identifiers):
        return fail("invalid_observation_identity")
    for number, binding in enumerate(image_bindings):
        if binding.get("evidence_id") not in identifiers or binding.get("attachment_index") != number:
            return fail("invalid_image_binding")
    payload = {"protocol": PROTOCOL, "agent": agent, "mode": "NORMAL" if mode == "normal" else "DEGRADED",
               "remaining_model_calls": remaining_model_calls, "tool_observations": [],
               "images": deepcopy(list(image_bindings))}
    scene = None
    for document in index["documents"].values():
        if isinstance(document, dict):
            for previous in document.get("tool_observations", []):
                if previous.get("scene_ref"):
                    scene = previous["scene_ref"]
                elif previous.get("tool", {}).get("name") == "reproduce" and previous.get("result", {}).get("error") not in {"draft_required", "browser_action_limit_8"}:
                    scene = previous["evidence_id"]
    for number, original in enumerate(observations):
        tool, raw = original.get("tool"), original.get("result")
        if not isinstance(tool, dict) or not isinstance(raw, dict) or tool.get("name", "") not in FIELDS:
            return fail("unsupported_observation_shape", evidence_id=original["evidence_id"])
        name = tool.get("name", "")
        root = f"/tool_observations/{number}"
        view = deepcopy(original)
        payload["tool_observations"].append(view)
        replacements, omitted = {}, {}

        def omit(path, reason):
            if _remove(view, path):
                omitted[path] = reason
                audit["omissions"].append({"evidence_id": original["evidence_id"], "raw_pointer": path, "reason": reason})

        def refer(path, reference, value):
            if size({"feedback_content_ref": reference})["utf8_bytes"] >= size(value)["utf8_bytes"]:
                return False
            parent, _, key = path.rpartition("/")
            found, container = _pointer(view, parent)
            if not found:
                return False
            container[key] = {"feedback_content_ref": reference}
            replacements[path] = reference
            audit["references"].append({"evidence_id": original["evidence_id"], "raw_pointer": path,
                                        "visible_pointer": root + path, "reference": reference})
            return True

        unknown = sorted(set(raw) - COMMON - FIELDS[name])
        audit["pending_validation"].extend({"evidence_id": original["evidence_id"], "field": k, "policy": "retained"} for k in unknown)
        if mode == "normal":
            ref = _request_reference(index, tool)
            if ref:
                if refer("/tool", ref, tool):
                    view["tool_name"] = name
        if name == "read_source":
            coverage = source_visibility(tool, raw)
            view["source_visibility"] = coverage
            if raw.get("ok") is True and coverage.get("reason"):
                return fail("required_source_text_unavailable", evidence_id=original["evidence_id"])
            if mode == "normal" and isinstance(raw.get("text"), str):
                ref = _source_reference(index, raw, coverage)
                if ref and refer("/result/text", ref, raw["text"]):
                    coverage["projection_omission"] = "exact_visible_reference"
        if name == "project_info":
            view["test_sources_completeness"] = "unknown_tool_returns_at_most_200"
        if name == "search_source":
            view["match_text_is_complete_source"] = False
        if name in {"reproduce", "interact"}:
            if name == "reproduce" and raw.get("error") not in {"draft_required", "browser_action_limit_8"}:
                scene = original["evidence_id"]
            view["scene_ref"] = scene
            view["session_identity"] = "not_provided_by_tool"
            view["failed_action"] = "unknown"
            omit("/result/browser/html", "default_html_not_directed_dom_query")
            if mode == "normal":
                for w, witness in enumerate(raw.get("witnesses", [])):
                    for t, layer in enumerate(witness.get("tiers", [])):
                        if layer.get("tier") != witness.get("tier") or any(k in witness and digest(witness[k]) != digest(v) for k, v in layer.items()):
                            continue
                        for key in sorted(RUNTIME_FIELDS & witness.keys() & layer.keys()):
                            if digest(witness[key]) == digest(layer[key]):
                                ref = _reference(index, index["next_message"], 0,
                                    root + f"/result/witnesses/{w}/tiers/{t}/{key}", layer[key], origin="same_observation_same_tier")
                                refer(f"/result/witnesses/{w}/{key}", ref, witness[key])
                        break
        for path in ("/result/build_stdout", "/result/build_stderr", "/result/node_stderr", "/result/browser/stderr", "/result/output_tail"):
            present, value = _pointer(view, path)
            if present and value == "":
                omit(path, "empty_diagnostic")
        artifact_fields = _artifact_paths(raw)
        audit["artifact_fields"].extend({"evidence_id": original["evidence_id"], "raw_pointer": "/result" + p}
                                        for p in artifact_fields if _pointer(raw, p)[0])
        if mode == "emergency":
            for path in artifact_fields:
                omit("/result" + path, "artifact_path_not_readable_content")
        for path, value in _leaves(original):
            exclusion = next((p for p in omitted if path == p or path.startswith(p + "/")), None)
            replacement = next((p for p in replacements if path == p or path.startswith(p + "/")), None)
            action = "omitted" if exclusion else "reference" if replacement else "retained"
            if action == "retained":
                found, shown = _pointer(view, path)
                if not found or digest(shown) != digest(value):
                    return fail("required_fact_not_preserved", evidence_id=original["evidence_id"], raw_pointer=path)
            audit["field_mapping"].append({"evidence_id": original["evidence_id"], "raw_pointer": path,
                "action": action, "visible_pointer": None if exclusion else root + (replacement or path),
                "reason": omitted.get(exclusion) if exclusion else None})
        metric = {"evidence_id": original["evidence_id"], "tool": name, "raw": size(original), "visible": size(view),
                  "original_tool_ok": raw.get("ok"), "required_facts_preserved": True,
                  "original_test_status": raw.get("status"), "original_browser_ok": raw.get("browser", {}).get("ok"),
                  "source_reference": "/result/text" in replacements,
                  "runtime_aliases": sum(p.startswith("/result/witnesses/") for p in replacements)}
        audit["observations"].append(metric)
    for item in audit["references"]:
        found, value = resolve_reference(item["reference"], index, payload)
        original = observations[identifiers.index(item["evidence_id"])]
        expected_found, expected = _pointer(original, item["raw_pointer"])
        if not found or not expected_found or digest(value) != digest(expected):
            return fail("unresolved_or_inexact_content_reference", evidence_id=item["evidence_id"])
    text = encode(payload)
    feedback = [{"type": "text", "text": text}, *deepcopy(list(image_parts))]
    audit["metrics"] = {"raw": size({"tool_observations": observations}), "visible": size(text),
                        "next_request": request_size(request_context, feedback), "tokens": None,
                        "reference_count": len(audit["references"]), "image_count": len(image_parts),
                        "images": {key: value for key, value in request_size({"messages": []}, feedback).items()
                                   if key.startswith("image_")}}
    oversized = [m["evidence_id"] for m in audit["observations"] if m["visible"]["utf8_bytes"] > config["max_observation_utf8_bytes"]]
    if oversized:
        return fail("required_observation_exceeds_budget", evidence_ids=oversized)
    if size(text)["utf8_bytes"] > config["max_feedback_utf8_bytes"]:
        return fail("required_feedback_exceeds_budget")
    if audit["metrics"]["next_request"]["utf8_bytes"] > config["max_request_text_utf8_bytes"]:
        return fail("required_request_exceeds_budget")
    audit["status"] = "ready" if mode == "normal" else "degraded"
    return feedback, audit


def construct_feedback(agent, observations, index, remaining_model_calls, budget, **context):

    attempts = []
    for mode in ("normal", "emergency"):
        try:
            feedback, audit = build_agent_feedback(agent, observations, index, remaining_model_calls,
                                                   budget, mode=mode, **context)
        except Exception as error:
            feedback = None
            audit = {"protocol": PROTOCOL, "agent": agent, "mode": mode, "status": "failed",
                     "failure": {"reason": "projection_exception", "type": type(error).__name__, "message": str(error)}}
        attempts.append(audit)
        if feedback is not None:
            return feedback, {"protocol": PROTOCOL, "status": audit["status"], "attempts": attempts,
                              "selected_attempt": len(attempts) - 1, "delivery": "prepared"}
    return None, {"protocol": PROTOCOL, "status": "unavailable", "attempts": attempts,
                  "selected_attempt": None, "delivery": "not_sent"}


def report_visibility(report, observations, audits):

    raw = {o["evidence_id"]: o for o in observations}
    shown = {}
    for audit in audits:
        if audit.get("delivery") != "sent_to_model":
            continue
        selected = audit["attempts"][audit["selected_attempt"]]
        shown.update({o["evidence_id"]: o for o in selected["observations"]})
    return {"claim_support": "not_evaluated", "claims": [
        {"hypothesis_id": c["hypothesis_id"], "evidence": [
            {"evidence_id": identifier, "in_raw": identifier in raw, "observation_visible": identifier in shown,
             "required_facts_visible": shown.get(identifier, {}).get("required_facts_preserved", False)}
            for identifier in c["evidence_ids"]]} for c in report.get("claims", [])]}
