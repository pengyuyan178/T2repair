import argparse
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root):
    manifest = read(root / "manifest.json")
    paths = []
    total = 0
    for record in manifest["records"]:
        path = (root / record["path"]).resolve()
        require(path.is_relative_to(root.resolve()), "Path outside the trace: " + record["path"])
        require(path.is_file(), "Missing file: " + record["path"])
        require(path.stat().st_size == record["bytes"], "Size mismatch: " + record["path"])
        require(sha256(path) == record["sha256"], "Hash mismatch: " + record["path"])
        paths.append(record["path"])
        total += record["bytes"]
    require(len(paths) == len(set(paths)) == 451, "Unexpected trace inventory")
    require(total == manifest["bytes"], "Trace byte count mismatch")

    evidence = read(root / "figure-evidence.json")
    provenance = read(root / "figure-provenance.json")
    require(provenance["evidence_sha256"] == sha256(root / "figure-evidence.json"), "Figure provenance mismatch")
    prefix = "result/method/causalgui/test480_deepseek/cases/PrismJS__prism-1853/"
    source_checks = 0
    for source, expected in evidence["source_sha256"].items():
        if source.startswith(prefix):
            require(sha256(root / source[len(prefix):]) == expected, "Figure evidence mismatch: " + source)
            source_checks += 1
    official = read(root / "official-result.json")
    require(official["source_sha256"] == evidence["source_sha256"]["result/method/causalgui/test480_deepseek/final_results.csv"], "Official result source mismatch")
    require(official["record"]["case_id"] == manifest["case"], "Official result case mismatch")
    require(official["record"]["官方是否通过"] == "是" and official["record"]["评测状态"] == "evaluated", "Official pass record missing")
    require(official["record"]["严格核验状态"] == "unconfirmed", "Strict audit status changed")

    for role, calls in [("code", 2), ("browser", 3)]:
        report = read(root / f"trajectory/agents/{role}/report.json")
        require(report["calls"] == calls, role + " call count mismatch")
        require(len(report["report"]["claims"]) == 8 and all(c["citations_valid"] for c in report["report"]["claims"]), role + " evidence references mismatch")
    require(read(root / "trajectory/S3b/replay_plan.json") == read(root / "trajectory/probe_draft.json"), "Frozen replay plan changed")
    require(read(root / "trajectory/S3b/replay_status.json")["model_calls_in_replay"] == 0, "Replay used model calls")
    result = read(root / "case_result.json")
    require(result["selected_candidate"] == evidence["selected_candidate"] == 1, "Selected candidate mismatch")
    require(len(list((root / "trajectory/candidates").glob("*/candidate.patch"))) == evidence["candidate_count"] == 5, "Candidate count mismatch")
    patch = (root / "patch.diff").read_bytes()
    require(patch == (root / "trajectory/candidates/01/candidate.patch").read_bytes(), "Selected patch mismatch")
    require(patch.decode("utf-8") == evidence["patch"], "Figure patch mismatch")

    base = json.loads(read(root / "trajectory/agents/browser/turn_02/tool_01/browser/observation.json")["dom"])
    replay = json.loads(read(root / "trajectory/candidates/01/validation/replay/T2/replay/observation.json")["dom"])
    require(base["tokens_s2"] == evidence["browser_agent"]["base_tokens_issue_input"], "Base tokens mismatch")
    require(replay["tokens_s2"] == evidence["replay_on_selected_candidate"]["tokens_issue_input"], "Replay tokens mismatch")
    require(not base["has_comment_s2"] and not any(t["type"] == "comment" for t in base["tokens_s2"]), "Base issue unexpectedly contains a comment token")
    pairs = lambda tokens: [(t["type"], t["content"]) for t in tokens]
    require(pairs(base["tokens_s2"])[9:12] == [("text", '"C'), ("string", '":"'), ("text", 'C"')], "Base split mismatch")
    require(pairs(replay["tokens_s2"])[9:12] == [("property", '"C"'), ("operator", ":"), ("string", '"C"')], "Repaired member mismatch")
    for control in ["real_line_comment", "real_block_comment", "control_only_C", "control_x_then_C", "control_slash_then_C"]:
        require(base[control] == replay[control], "Preservation control changed: " + control)
    for name, expected in [("real_line_comment", "// tail"), ("real_block_comment", "/* block */")]:
        require(("comment", expected) in pairs(replay[name]), "Comment control missing")
    validation = read(root / "trajectory/candidates/01/validation.json")
    require(validation["probe_improved"] is False and validation["target_improved"] is False, "Scalar improvement flags changed")
    require(validation["symbol_validation"] == "UNKNOWN", "Unknown symbol check changed")
    return {"case": manifest["case"], "verified_files": len(paths), "verified_bytes": total,
            "figure_source_files_verified": source_checks, "selected_candidate": 1,
            "code_calls": 2, "browser_calls": 3, "model_calls_in_replay": 0,
            "base_split_confirmed": True, "repaired_tokens_confirmed": True,
            "comment_controls_preserved": True, "official_pass_record": True,
            "strict_audit_status": "unconfirmed", "new_model_calls": 0, "new_evaluation_runs": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify the archived Fig. 8 trace offline, without model calls or rerunning the benchmark.")
    parser.add_argument("--trace", type=Path, default=Path(__file__).resolve().parents[1] / "trajectories/fig8-prism-1853")
    print(json.dumps(verify(parser.parse_args().trace), indent=2))
