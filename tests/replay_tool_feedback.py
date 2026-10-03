import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from causalgui.feedback import (PROTOCOL, construct_feedback, digest, encode, feedback_config,
                               request_size, size, visible_context_index)
from causalgui.main import agent_turn_message


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def original_request(trajectory, agent, turn):

    paths = [p for p in sorted(trajectory.glob(f"*S3_{agent}_{turn}_*request.json"))
             if "_s01_" not in p.name or "_r0_" in p.name]
    return read(paths[0]) if len(paths) == 1 else None


def prepare(batch, output):

    assert not output.exists(), "Use a new immutable input directory"
    selection = read(batch / "selected_cases.json")
    settings = read(batch / "run_manifest.json")["config"]["worker"]["metadata"]
    identifiers = selection["instance_ids"]
    assert len(identifiers) == len(set(identifiers))
    output.mkdir(parents=True)
    manifest = {"protocol": "tool_feedback_replay_inputs_v1", "source_batch": str(batch), "seed": 42,
                "selection_sha256": file_digest(batch / "selected_cases.json"), "model_calls": 0,
                "gold_included": False, "future_answers_included": False, "cases": [],
                "frozen_model_settings": {k: settings.get(k) for k in
                    ("model", "model_transport", "harness_policy", "output_contract", "per_choice_output_limit", "http_retry_policy", "agent_limits")}}
    for identifier in identifiers:
        trajectory = batch / "cases" / identifier / "trajectory"
        data = {"instance_id": identifier, "agents": {}}
        for agent in ("code", "browser"):
            folder = trajectory / "agents" / agent
            files = sorted(folder.glob("turn_*/tool_*/observation.json"))
            raw_by_id = {read(p)["evidence_id"]: {"value": read(p), "sha256": file_digest(p)} for p in files}
            role = {"status": "ready", "raw_files": len(files), "rounds": [], "raw_without_feedback": [],
                    "raw_file_sha256": {k: v["sha256"] for k, v in raw_by_id.items()}}
            data["agents"][agent] = role
            if not (folder / "history.json").is_file():
                role.update(status="upstream_history_unavailable", raw_without_feedback=[v["value"] for v in raw_by_id.values()])
                continue
            history = read(folder / "history.json")
            limit = settings.get("agent_limits", {}).get(agent, 2 if agent == "code" else 3)
            seen = set()
            for position, message in enumerate(history):
                parts = message.get("content")
                if message.get("role") != "user" or not isinstance(parts, list) or not parts or parts[0].get("type") != "text":
                    continue
                document = json.loads(parts[0]["text"])
                if "tool_observations" not in document:
                    continue
                observations = document["tool_observations"]
                if not observations:
                    continue
                turn = int(observations[0]["evidence_id"].split(".t", 1)[1].split(".", 1)[0])
                record = {"turn": turn, "position": position, "status": "ready", "observations": observations,
                          "history_prefix": history[:position], "old_feedback": parts, "remaining_model_calls": limit - turn}
                role["rounds"].append(record)
                missing = []
                for observation in observations:
                    identity = observation["evidence_id"]
                    if identity.endswith(".limit"):
                        continue
                    seen.add(identity)
                    if identity not in raw_by_id:
                        missing.append(identity)
                    else:
                        assert raw_by_id[identity]["value"] == observation, identity
                if missing:
                    record.update(status="upstream_raw_unavailable", missing=missing)
                    continue
                request = original_request(trajectory, agent, turn + 1)
                record["next_request_archived"] = request is not None
                if request is not None:
                    assert request["messages"][1:position + 1] == history[:position]
                    assert request["messages"][position + 1] == message
                else:
                    request = original_request(trajectory, agent, turn)
                if request is None:
                    record["status"] = "upstream_request_unavailable"
                    continue
                picture_candidates = [o["evidence_id"] for o in observations if o["result"].get("screenshot")]
                pictures = parts[1:]
                if len(picture_candidates) != len(pictures) or any(p.get("type") != "image_url" for p in pictures):
                    record["status"] = "image_binding_unavailable"
                    continue
                record.update(image_parts=pictures, image_bindings=[{"evidence_id": evidence, "attachment_index": i}
                    for i, evidence in enumerate(picture_candidates)],
                    system_message=request["messages"][0], response_format=request.get("response_format"),
                    next_turn=request["messages"][position + 2] if record["next_request_archived"] else agent_turn_message(turn + 1, limit),
                    request_parameters={k: v for k, v in request.items() if k not in {"messages", "response_format"}})
            role["raw_without_feedback"] = [v["value"] for k, v in raw_by_id.items() if k not in seen]
            if not files and not role["rounds"]:
                role["status"] = "no_tool_calls"
            elif not role["rounds"]:
                role["status"] = "upstream_feedback_unavailable"
        target = output / (identifier + ".json.gz")
        target.write_bytes(gzip.compress(encode(data).encode("utf-8"), mtime=0))
        manifest["cases"].append({"instance_id": identifier, "input_sha256": file_digest(target),
                                  "agents": {a: {"status": r["status"], "raw_files": r["raw_files"], "feedback_rounds": len(r["rounds"]),
                                      "raw_without_feedback": len(r["raw_without_feedback"])} for a, r in data["agents"].items()}})
    write(output / "manifest.json", manifest)
    return manifest


def distribution(values):
    ordered = sorted(values)
    return {"n": len(values), "sum": sum(values), "median": statistics.median(values) if values else None,
            "max": max(values) if values else None,
            **{f"p{q}": ordered[max(0, math.ceil(len(ordered) * q / 100) - 1)] if ordered else None for q in (90, 95, 99)}}


def summarize(rows):

    statuses = Counter()
    groups = defaultdict(list)
    next_requests = defaultdict(list)
    omissions = Counter()
    for case in rows:
        for agent, role in case["agents"].items():
            statuses[f"{agent}:{role['status']}"] += 1
            for turn in role["rounds"]:
                statuses[f"round:{agent}:{turn['status']}"] += 1
                for item in turn.get("observations", []):
                    groups[(agent, item["tool"], turn["turn"])].append(item)
                if "next_request" in turn:
                    next_requests[agent].append(turn)
                omissions.update(turn.get("omission_reasons", []))
    measured = []
    for (agent, tool, turn), items in sorted(groups.items()):
        measured.append({"agent": agent, "tool": tool, "turn": turn, "n": len(items),
            "original_tool_failures": sum(i["original_tool_ok"] is False for i in items),
            "original_browser_action_failures": sum(i["original_browser_ok"] is False for i in items),
            "original_failed_tests": sum(i["original_test_status"] == "FAIL" for i in items),
            "source_references": sum(i["source_reference"] for i in items),
            "runtime_aliases": sum(i["runtime_aliases"] for i in items),
            "required_facts_preserved": all(i["required_facts_preserved"] for i in items),
            **{side: {unit: distribution([i[side][unit] for i in items]) for unit in ("chars", "utf8_bytes")}
               for side in ("raw", "visible")}})
    return {"planned_cases": len(rows), "planned_agents": len(rows) * 2, "model_calls": 0, "tool_calls": 0, "tokens": None,
            "original_tool_failures_in_frozen_rounds": sum(t.get("original_tool_failures", 0) for c in rows
                for r in c["agents"].values() for t in r["rounds"]),
            "statuses": dict(sorted(statuses.items())), "observation_groups": measured, "omissions": dict(omissions),
            "next_request_groups": {a: {side: {unit: distribution([r[side][unit] for r in turns if r[side][unit] is not None])
                for unit in ("chars", "utf8_bytes", "image_count", "image_url_utf8_bytes", "image_binary_bytes")}
                for side in ("old_next_request", "next_request")} for a, turns in next_requests.items()},
            "distribution_population": "available frozen input subset; missing cases retained in manifest", "cases": rows}


def replay(inputs, output, config):

    assert not output.exists(), "Use a new output directory"
    output.mkdir(parents=True)
    manifest = read(inputs / "manifest.json")
    assert manifest["gold_included"] is False and manifest["future_answers_included"] is False
    rows = []
    for item in manifest["cases"]:
        source = inputs / (item["instance_id"] + ".json.gz")
        assert file_digest(source) == item["input_sha256"]
        data = json.loads(gzip.decompress(source.read_bytes()))
        case = {"instance_id": data["instance_id"], "agents": {}}
        for agent, frozen in data["agents"].items():
            role = {"status": frozen["status"], "raw_files": frozen["raw_files"],
                    "raw_without_feedback": len(frozen["raw_without_feedback"]), "rounds": []}
            case["agents"][agent] = role
            replacements, stopped = {}, False
            for turn in frozen["rounds"]:
                result = {"turn": turn["turn"], "status": turn["status"],
                          "original_tool_failures": sum(o["result"].get("ok") is False for o in turn["observations"])}
                role["rounds"].append(result)
                if stopped:
                    result["status"] = "not_replayed_after_agent_stop"
                    continue
                if turn["status"] != "ready":
                    stopped = True
                    continue
                history = deepcopy(turn["history_prefix"])
                for position, feedback in replacements.items():
                    history[position]["content"] = feedback
                context = {"messages": [turn["system_message"], *history], "response_format": turn["response_format"], "next_turn": turn["next_turn"]}
                arguments = (agent, turn["observations"], visible_context_index(agent, history), turn["remaining_model_calls"], config[agent])
                before = digest(turn["observations"])
                first = construct_feedback(*arguments, request_context=context, image_parts=turn["image_parts"], image_bindings=turn["image_bindings"])
                second = construct_feedback(*arguments, request_context=context, image_parts=turn["image_parts"], image_bindings=turn["image_bindings"])
                assert first == second and before == digest(turn["observations"])
                feedback, audit = first
                directory = output / "cases" / data["instance_id"] / agent / f"turn_{turn['turn']:02d}"
                audit.update(delivery="offline_not_sent", turn=turn["turn"])
                write(directory / "audit.json", audit)
                result.update(status=audit["status"], stable=True, raw_unchanged=True)
                if feedback is None:
                    stopped = True
                    role["status"] = "feedback_unavailable"
                    continue
                replacements[turn["position"]] = feedback
                assert feedback[1:] == turn["image_parts"]
                write(directory / "feedback.json", feedback)
                selected = audit["attempts"][audit["selected_attempt"]]
                result.update(observations=selected["observations"], next_request=selected["metrics"]["next_request"],
                    old_next_request=request_size({**context, "messages": [turn["system_message"], *turn["history_prefix"]]}, turn["old_feedback"]),
                    omission_reasons=[o["reason"] for o in selected["omissions"]], visible_references=len(selected["references"]))
        rows.append(case)
        write(output / "cases" / data["instance_id"] / "comparison.json", case)
    summary = summarize(rows)
    summary.update(protocol=PROTOCOL, input_manifest_sha256=file_digest(inputs / "manifest.json"), budget=config)
    write(output / "summary.json", summary)
    return summary


def prepare_pairs(inputs, results, output):

    assert not output.exists(), "Use a new paired input directory"
    manifest = read(inputs / "manifest.json")
    result_summary = read(results / "summary.json")
    assert result_summary["input_manifest_sha256"] == file_digest(inputs / "manifest.json")
    candidates = {"code": [], "browser": []}
    for item in manifest["cases"]:
        source = inputs / (item["instance_id"] + ".json.gz")
        assert file_digest(source) == item["input_sha256"]
        data = json.loads(gzip.decompress(source.read_bytes()))
        for agent, role in data["agents"].items():
            for turn in role["rounds"]:
                folder = results / "cases" / item["instance_id"] / agent / f"turn_{turn['turn']:02d}"
                if turn["status"] != "ready" or not (folder / "feedback.json").is_file():
                    continue
                history = turn["history_prefix"]
                context = {"messages": [turn["system_message"], *history], "response_format": turn["response_format"], "next_turn": turn["next_turn"]}
                feedback, audit = construct_feedback(agent, turn["observations"], visible_context_index(agent, history),
                    turn["remaining_model_calls"], result_summary["budget"][agent],
                    request_context=context, image_parts=turn["image_parts"], image_bindings=turn["image_bindings"])
                if feedback is None:
                    continue
                features = set(o["tool"].get("name", "batch_notice") for o in turn["observations"])
                if turn["remaining_model_calls"] == 1:
                    features.add("final_summary")
                if turn["image_parts"]:
                    features.add("screenshots")
                if any(o["result"].get("ok") is False or o["result"].get("browser", {}).get("ok") is False for o in turn["observations"]):
                    features.add("failure")
                if any(o["source_reference"] for o in audit["attempts"][audit["selected_attempt"]]["observations"]):
                    features.add("source_reference")
                candidates[agent].append((item["instance_id"], turn, feedback, features))
    selected = []
    randomizer = random.Random(42)
    for agent, choices in candidates.items():
        randomizer.shuffle(choices)
        covered, used = set(), set()
        for _ in range(5):
            available = [x for x in choices if x[0] not in used]
            if not available:
                break
            choice = max(available, key=lambda x: (len(x[3] - covered), size(x[1]["old_feedback"][0]["text"])["utf8_bytes"]))
            identifier, turn, feedback, features = choice
            used.add(identifier)
            covered.update(features)
            selected.append((agent, identifier, turn, feedback, sorted(features)))
    orders = ["AB", "BA"] * ((len(selected) + 1) // 2)
    randomizer.shuffle(orders)
    records = []
    for i, (agent, identifier, turn, feedback, features) in enumerate(selected):
        pair_id = f"{i + 1:02d}_{agent}_{identifier}_t{turn['turn']}"
        paths = {}
        for arm, content in (("A", turn["old_feedback"]), ("B", feedback)):
            system = deepcopy(turn["system_message"])
            system["content"] += ("\nBoth raw tool_observations and tool_feedback_v1 are supported. feedback_content_ref points to "
                "the same Agent's zero-based message/part and JSON Pointer, optionally a character text_slice [start,end). "
                "Artifact paths are not readable content. DEGRADED preserves required evidence. Tool ok alone is neither "
                "test PASS nor browser reproduction success. Missing/partial evidence remains unknown.")
            body = {**turn["request_parameters"], "messages": [system, *turn["history_prefix"],
                    {"role": "user", "content": content}, turn["next_turn"]]}
            if turn["response_format"] is not None:
                body["response_format"] = turn["response_format"]
            target = output / pair_id / (arm + ".json")
            write(target, body)
            paths[arm] = {"path": f"{pair_id}/{arm}.json", "sha256": file_digest(target)}
        records.append({"pair_id": pair_id, "agent": agent, "instance_id": identifier, "turn": turn["turn"],
                        "features": features, "order": orders[i], "requests": paths, "rating": "blind_review_pending"})
    write(output / "manifest.json", {"protocol": "paired_tool_feedback_v1", "status": "prepared_not_executed", "seed": 42,
        "requested_pairs": 10, "prepared_pairs": len(records), "pairs": records, "model_calls": 0,
        "input_manifest_sha256": file_digest(inputs / "manifest.json"), "frozen_model_settings": manifest["frozen_model_settings"],
        "execution": "Separate explicit authorization and frozen Docker broker required; no run mode in this offline script."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "replay", "prepare-pairs"))
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.batch, args.out)
    elif args.mode == "replay":
        replay(args.inputs, args.out, feedback_config(read(args.config) if args.config else None))
    else:
        prepare_pairs(args.inputs, args.results, args.out)
