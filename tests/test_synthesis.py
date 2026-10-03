import hashlib
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from causalgui.synthesis import PATCH_PROTOCOL, apply_candidate
from causalgui.s5_context import construct_synthesis_payload, reference_errors, s5_context_config


class AtomicEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="causalgui-test-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("config", "core.autocrlf", "false")
        self.write("main.js", "const a = 1;\nconst b = 2;\nexport { a, b };\n")
        self.write("style.scss", "$tone: red;\n.button { color: $tone; }\n")
        self.write("effect.frag", "uniform float alpha;\nvoid main() {}\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def git(self, *args, data=None):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())

    def snapshot(self):
        return {str(path.relative_to(self.repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.repo.rglob("*") if path.is_file()}

    def apply(self, edits, **kwargs):
        before = self.snapshot()
        result = apply_candidate(self.repo, edits, **kwargs)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(result["patch_protocol"], PATCH_PROTOCOL)
        if not result["valid"]:
            self.assertEqual((result["patch"], result["changed_files"]), ("", []))
        return result

    def test_atomic_group_new_shader_and_scoped_edit(self):
        result = self.apply([
            {"file": "main.js", "search": "a = 1", "replace": "a = 10"},
            {"file": "main.js", "search": "b = 2", "replace": "b = 20"},
            {"file": "style.scss", "search": "red", "replace": "blue"},
            {"file": "shaders/new.vert", "create": True, "search": "", "replace": "void main() {}\n"},
            {"file": "effect.frag", "search": "float alpha", "replace": "float opacity"},
        ], allowed_files=["main.js", "style.scss", "shaders/new.vert", "effect.frag"],
            required_groups=[["main.js", "style.scss", "shaders/new.vert"]])
        self.assertTrue(result["valid"], result)
        self.assertIn("new file mode 100644", result["patch"])
        self.assertIn("+const a = 10;\n+const b = 20;", result["patch"])
        self.assertIn("diff --git a/effect.frag b/effect.frag", result["patch"])
        self.git("apply", "--check", "-", data=result["patch"].encode())

    def test_candidate_one_shot_applies_to_its_supplied_source(self):
        from causalgui.main import Candidate, output_one_shot
        example = output_one_shot(Candidate.model_json_schema())
        for window in example["input"]["source_windows"]:
            self.write(window["file"], window["text"])
        self.git("add", ".")
        self.git("commit", "-qm", "one-shot base")
        result = self.apply(example["output"]["edits"],
                            allowed_files=example["input"]["edit_contract"]["allowed_change"])
        self.assertTrue(result["valid"], result)
        self.git("apply", "--check", "-", data=result["patch"].encode())

    def test_rejects_duplicate_overlap_ambiguity_and_windows(self):
        first = {"file": "main.js", "search": "a = 1", "replace": "a = 10"}
        cases = [
            ([first, first], "duplicate_edit"),
            ([first, {"file": "main.js", "search": "1;", "replace": "3;"}], "overlapping_edits"),
            ([{"file": "main.js", "search": "const", "replace": "let"}], "ambiguous_search"),
            ([dict(first, search="missing value")], "search_not_found"),
            ([dict(first, start_line=2, end_line=2)], "unsupported_line_window"),
            ([dict(first, start_line=1)], "unsupported_line_window"),
            ([dict(first, end_line=None)], "unsupported_line_window"),
            ([dict(first, replace=first["search"])], "no_op_edit"),
        ]
        for edits, reason in cases:
            with self.subTest(reason=reason):
                result = self.apply(edits)
                self.assertFalse(result["valid"])
                self.assertTrue(result["reason"].startswith(reason), result)

    def test_original_coordinates_and_exact_search(self):
        result = self.apply([
            {"file": "main.js", "search": "const a = 1;", "replace": "const a = 10;\nconst added = 3;"},
            {"file": "main.js", "search": "const b = 2;", "replace": "let b = 2;"},
        ])
        self.assertTrue(result["valid"], result)
        self.assertIn("+let b = 2;", result["patch"])
        self.assertEqual([row["start_line"] for row in result["locations"]], [1, 2])
        self.git("apply", "--check", "-", data=result["patch"].encode())
        self.assertFalse(self.apply([{"file": "main.js", "search": "const a=1;", "replace": "x"}])["valid"])

    def test_whole_file_unique_search_records_original_hash_and_positions(self):
        source = "const first = 1;\n" + "// middle\n" * 510 + "const last = 2;"
        self.write("main.js", source)
        self.git("add", "main.js")
        self.git("commit", "-qm", "whole file fixture")
        edits = [{"file": "main.js", "search": "const first = 1;", "replace": "const first = 3;"},
                 {"file": "main.js", "search": "const last = 2;", "replace": "const last = 4;"}]
        result = self.apply(edits)
        self.assertTrue(result["valid"], result)
        self.assertEqual([row["start_line"] for row in result["locations"]], [1, 512])
        for edit, row in zip(edits, result["locations"]):
            self.assertEqual(row["source_sha256"], hashlib.sha256(source.encode()).hexdigest())
            self.assertEqual(row["match_count"], 1)
            self.assertEqual(source.encode()[row["start_byte"]:row["end_byte"]], edit["search"].encode())
        self.git("apply", "--check", "-", data=result["patch"].encode())

    def test_repeated_and_overlapping_matches_require_unique_context(self):
        source = "const first = 'aaa';\nconst second = 'aaa';\n"
        self.write("main.js", source)
        self.git("add", "main.js")
        self.git("commit", "-qm", "repeated fixture")
        for search, count in [("aa", 4), ("'aaa'", 2), ("missing", 0)]:
            result = self.apply([{"file": "main.js", "search": search, "replace": "'bbb'"}])
            self.assertFalse(result["valid"])
            row = result["locations"][0]
            self.assertEqual(row["match_count"], count)
            self.assertIsNone(row["start_byte"])
            self.assertIsNone(row["start_line"])
            self.assertEqual(row["source_sha256"], hashlib.sha256(source.encode()).hexdigest())
        result = self.apply([{"file": "main.js", "search": "const second = 'aaa';", "replace": "const second = 'bbb';"}])
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["locations"][0]["start_line"], 2)

    def test_utf8_and_line_endings_are_preserved_exactly(self):
        for newline in ["\n", "\r\n", "\r"]:
            with self.subTest(newline=repr(newline)):
                prefix = "// 中文" + newline
                search = "\tconst label = '你好';" + newline
                self.write("main.js", prefix + search + "const tail = 2;" + newline)
                self.git("add", "main.js")
                self.git("commit", "-qm", "encoding fixture")
                result = self.apply([{"file": "main.js", "search": search, "replace": "\tconst label = '您好';" + newline}])
                self.assertTrue(result["valid"], result)
                row = result["locations"][0]
                self.assertEqual((row["start_line"], row["end_line"]), (2, 2))
                self.assertEqual(row["start_byte"], len(prefix.encode()))
                self.assertEqual(row["end_byte"], len((prefix + search).encode()))
                self.git("apply", "--check", "-", data=result["patch"].encode())
                wrong = search.replace("\t", "    ")
                self.assertFalse(self.apply([{"file": "main.js", "search": wrong, "replace": "x"}])["valid"])
                if newline != "\n":
                    self.assertFalse(self.apply([{"file": "main.js", "search": search.replace(newline, "\n"), "replace": "x"}])["valid"])

    def test_later_failure_discards_already_located_edits(self):
        result = self.apply([
            {"file": "main.js", "search": "const a = 1;", "replace": "const a = 10;"},
            {"file": "style.scss", "search": "missing", "replace": "blue"},
        ])
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "search_not_found:style.scss")
        self.assertEqual([row["match_count"] for row in result["locations"]], [1, 0])

    def test_edit_cannot_search_text_created_by_another_edit(self):
        result = self.apply([
            {"file": "main.js", "search": "const a = 1;", "replace": "const a = 10;\nconst added = 3;"},
            {"file": "main.js", "search": "const added = 3;", "replace": "const added = 4;"},
        ])
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "search_not_found:main.js")

    def test_rejects_incomplete_group_scope_and_invalid_creation(self):
        edit = {"file": "main.js", "search": "a = 1", "replace": "a = 10"}
        self.assertFalse(self.apply([edit], required_groups=[["main.js", "style.scss"]])["valid"])
        self.assertFalse(self.apply([edit], allowed_files=["style.scss"])["valid"])
        invalid = [
            {"file": "new.js", "search": "", "replace": "x"},
            {"file": "new.js", "create": True, "search": "", "replace": ""},
            {"file": "main.js", "search": "", "replace": "x"},
            {"file": "main.js", "create": True, "search": "", "replace": "x"},
            {"file": "new.js", "create": True, "search": "", "replace": "x", "start_line": None},
        ]
        for candidate in invalid:
            self.assertFalse(self.apply([edit, candidate])["valid"])

    def test_path_escape_and_symbolic_link(self):
        for name in ["../escape.js", "/tmp/escape.js", ".git/config", "a/../../x", "C:/x.js", "a\\x.js"]:
            self.assertFalse(self.apply([{"file": name, "create": True, "search": "", "replace": "x"}])["valid"])
        (self.repo / "linked.js").symlink_to(self.repo / "main.js")
        result = self.apply([{"file": "linked.js", "search": "a = 1", "replace": "a = 10"}])
        self.assertTrue(result["reason"].startswith("unsafe_path"))

    def test_preserves_unrelated_staged_and_untracked_data(self):
        self.write("style.scss", "$tone: green;\n")
        self.git("add", "style.scss")
        self.write("notes.txt", "untracked fixture\n")
        result = self.apply([{"file": "main.js", "search": "a = 1", "replace": "a = 10"}])
        self.assertTrue(result["valid"], result)
        self.assertNotIn("style.scss", result["patch"])
        self.assertNotIn("notes.txt", result["patch"])
        self.assertFalse(self.apply([{"file": "style.scss", "search": "green", "replace": "blue"}])["valid"])


class S5ContextTests(unittest.TestCase):
    git = AtomicEditTests.git
    write = AtomicEditTests.write
    snapshot = AtomicEditTests.snapshot
    apply = AtomicEditTests.apply

    def setUp(self):
        AtomicEditTests.setUp(self)
        self.state = self.make_state()

    def make_state(self, line=2):
        from causalgui.main import make_plan, rank_hypotheses
        constraints = [{"id": "R1", "kind": "REQUIREMENT", "role": "CHANGE_DISTINGUISH", "entity": "value",
            "property": "b", "context": "when active", "relation": "== 3", "strength": "MAY",
            "ambiguity_reason": "fixture_uncertainty", "provenance_valid": False,
            "provenance": {"kind": "issue_sentence", "reference": "Make active values three.", "region": []}},
            {"id": "R2", "kind": "FRAME", "role": "PRESERVE", "entity": "value", "property": "a",
             "context": "globally", "relation": "unchanged", "strength": "MUST", "ambiguity_reason": "",
             "provenance_valid": True, "provenance": {"kind": "issue_sentence", "reference": "Keep a.", "region": []}}]
        spec = {"entities": ["value"], "constraints": constraints, "ambiguity_groups": []}
        h = {"id": "H1", "boundary_id": "", "boundary_valid": False, "file": "main.js", "symbol": "b", "line": line,
             "mechanism": "output_path", "question": {"kind": "value", "expression": "b", "expected": "2"},
             "readable_symbols": ["a", "b"], "target_property": "b", "constraint_ids": ["R1"]}
        scope = {"allowed_files": ["main.js", "style.scss"], "context": [], "bindings": {}, "edges": [],
                 "unresolved_dependencies": [], "truncated": True, "baseline_topk": ["main.js"]}
        root = rank_hypotheses([h], [], {}, ["main.js"])
        plan = {**make_plan(scope, spec, {"new_files": ["new.js"]}, root), "patch_protocol": PATCH_PROTOCOL}
        return {"task": {"issue": "Make active values three. Keep a.", "repo": "fixture/local",
                         "base_commit": self.git("rev-parse", "HEAD").decode().strip()},
                "spec": spec, "scope": scope, "root": root, "plan": plan, "witnesses": [], "draft": {},
                "front_context": {"selected_boundaries": []}, "s0_facts": {"files": {}}, "images": [],
                "initial_payloads": {}, "agent_audits": {},
                "reports": {agent: {"agent": agent, "calls": 1, "call_limit": 2 if agent == "code" else 3,
                    "stop_reason": "completed", "report": {"summary": "No runtime conclusion.", "claims": [], "limitations": []},
                    "observations": []} for agent in ("code", "browser")}}

    def build(self, **kwargs):
        return construct_synthesis_payload(self.repo, **self.state, **kwargs)

    def raw_read(self, file="main.js", agent="code", start=1, end=240):
        from causalgui.code_agent import SourceTools
        tools = SourceTools(self.repo, {})
        tool = {"name": "read_source", "source_id": tools.ids[file], "start_line": start, "end_line": end, "query": ""}
        raw = tools.execute(tool, self.repo.parent / (agent + "_tool"))
        observation = {"evidence_id": agent + ".t1.o1", "tool": tool, "result": raw}
        self.state["reports"][agent]["observations"].append(observation)
        return observation, tools

    def test_independent_full_state_and_read_permissions(self):
        observation, tools = self.raw_read()
        before, files, tree = deepcopy(self.state), list(tools.read_files), self.snapshot()
        payload, audit = self.build()
        self.assertIsNotNone(payload, audit)
        self.assertEqual(self.state, before)
        self.assertEqual(files, tools.read_files)
        self.assertEqual(tree, self.snapshot())
        self.assertEqual((payload, audit), self.build())
        payload["requirements"]["constraints"][0]["strength"] = "MUST"
        self.assertEqual(self.state, before)

    def test_requirements_once_and_original_diagnosis_unchanged(self):
        self.state["spec"]["constraints"].append(deepcopy(self.state["spec"]["constraints"][0]))
        payload, audit = self.build()
        self.assertEqual(len(payload["requirements"]["constraints"]), 2)
        self.assertEqual(payload["requirements"]["constraints"], self.state["spec"]["constraints"][:2])
        self.assertEqual(payload["edit_contract"]["must_preserve"], ["R2"])
        self.assertEqual(payload["edit_contract"]["may_explain"], ["R1"])
        self.assertEqual(audit["attempts"][0]["deduplicated_constraints"], 1)
        h = payload["hypotheses"]["items"][0]
        for key in ("id", "line", "verdict", "question", "baseline_rank", "input_rank"):
            self.assertEqual(h[key], self.state["root"]["hypotheses"][0][key])

    def test_conflicting_requirements_are_not_overwritten(self):
        self.state["spec"]["constraints"].append({**self.state["spec"]["constraints"][0], "strength": "MUST"})
        payload, audit = self.build()
        self.assertIsNone(payload)
        self.assertEqual(audit["failure"]["reason"], "constraint_id_conflict")

    def test_deep_target_and_unique_context_come_from_base(self):
        text = "function big() {\n" + "  const repeated = 0;\n" * 800 + "  return '深层';\n}\n"
        self.write("main.js", text)
        self.git("add", "main.js")
        self.git("commit", "-qm", "deep source")
        self.state = self.make_state(802)
        self.state["s0_facts"]["files"]["main.js"] = {"source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "functions": [{"name": "big", "line": 1, "end_line": 803}]}
        payload, audit = self.build()
        self.assertIsNotNone(payload, audit)
        self.assertTrue(audit["metrics"]["coverage"][0]["unique_edit_window"])
        self.assertTrue(any("深层" in w["text"] for w in payload["source_windows"]))
        self.assertLess(audit["metrics"]["source_utf8_bytes"], len(text.encode()))

    def test_post_agent_read_and_initial_only_window_are_handed_off(self):
        raw, tools = self.raw_read("style.scss", agent="browser")
        self.state["initial_payloads"]["code"] = {"source_windows": [{"file": "effect.frag", "start_line": 1, "end_line": 2,
            "source_sha256": hashlib.sha256((self.repo / "effect.frag").read_bytes()).hexdigest()}]}
        payload, audit = self.build()
        self.assertEqual({w["file"] for w in payload["source_windows"]}, {"main.js", "style.scss", "effect.frag"})
        self.assertNotIn("effect.frag", payload["edit_contract"]["allowed_change"])
        self.assertEqual(tools.read_files, ["style.scss"])
        self.assertTrue(payload["evidence"]["tool:browser.t1.o1"]["source_refs"])

    def test_crlf_unicode_eof_and_overlapping_source_dedup(self):
        text = "// 中文\r\nconst a = 1;\r\n\tconst b = '你好';\r\nexport {a,b};"
        self.write("main.js", text)
        self.git("add", "main.js")
        self.git("commit", "-qm", "raw bytes")
        self.state = self.make_state(3)
        self.raw_read()
        window = {"file": "main.js", "start_line": 1, "end_line": 4}
        self.state["initial_payloads"] = {a: {"source_windows": [window]} for a in ("code", "browser")}
        payload, audit = self.build()
        self.assertEqual(payload["source_windows"][0]["text"], text)
        self.assertEqual(audit["metrics"]["duplicate_source_lines"], 0)
        search = payload["source_windows"][0]["text"]
        result = self.apply([{"file": "main.js", "search": search, "replace": search.replace("你好", "您好"), "create": False}],
                            allowed_files=payload["edit_contract"]["allowed_change"])
        self.assertTrue(result["valid"], result)

    def test_raw_character_cut_uses_actual_range(self):
        self.write("main.js", "// 中文" + "x" * 490 + "\r\n" + ("// " + "y" * 490 + "\r\n") * 98 + "const end = 1;")
        self.git("add", "main.js")
        self.git("commit", "-qm", "truncated tool")
        self.state = self.make_state(1)
        observation, _ = self.raw_read()
        self.assertEqual(len(observation["result"]["text"]), 24000)
        payload, audit = self.build()
        actual = audit["attempts"][0]["verified_reads"]["code.t1.o1"]
        self.assertLess(actual["end_line"], observation["result"]["end_line"])
        self.assertNotEqual(payload["evidence"]["tool:code.t1.o1"]["source_visibility"]["status"], "complete")
        self.assertTrue(payload["evidence"]["tool:code.t1.o1"]["content_complete"])
        self.assertFalse(any(g["kind"] == "source_not_fully_expanded" and g["origin"] == "tool:code.t1.o1"
                             for g in payload["gaps"]))

    def test_versions_and_optional_audit_do_not_destroy_other_source(self):
        observation, _ = self.raw_read()
        observation["result"]["sha256"] = "wrong-version"
        self.state["agent_audits"]["code"] = "unavailable audit"
        payload, audit = self.build()
        self.assertIsNotNone(payload, audit)
        self.assertEqual(payload["evidence"]["tool:code.t1.o1"]["source_refs"], [])
        self.assertTrue(any(g["kind"] == "source_version_conflict" for g in payload["gaps"]))
        self.assertTrue(any(g["kind"] == "audit_unavailable" for g in payload["gaps"]))

    def test_unavailable_agent_preserves_raw_without_fabricated_conclusion(self):
        self.raw_read()
        agent = self.state["reports"]["code"]
        agent.update(stop_reason="feedback_unavailable", report={"summary": "Controller status", "claims": [], "limitations": ["feedback failed"]})
        self.state["agent_audits"]["code"] = {"feedback": [{"attempts": [{"failure": {"reason": "too_large"}}]}]}
        payload, audit = self.build()
        self.assertEqual(payload["mode"], "NORMAL")
        self.assertFalse(payload["agents"]["code"]["model_report_available"])
        self.assertTrue(payload["agents"]["code"]["evidence_refs"])
        self.assertEqual(payload["agents"]["code"]["claims"], [])
        self.assertEqual(payload["agents"]["code"]["feedback_failures"], [{"reason": "too_large"}])
        self.assertEqual(payload["hypotheses"]["items"][0]["verdict"], "UNKNOWN")

    def test_unversioned_index_is_distinct_from_conflicting_source(self):
        for digest, kind in ((None, "static_index_unversioned"), ("wrong-version", "source_version_conflict")):
            with self.subTest(digest=digest):
                self.state["s0_facts"]["files"]["main.js"] = {"source_sha256": digest,
                    "functions": [{"name": "unverified", "line": 1000, "end_line": 1001}]}
                before = deepcopy(self.state)
                payload, audit = self.build()
                self.assertIsNotNone(payload, audit)
                version_gaps = [g for g in payload["gaps"] if g.get("reason") == "static_index_version"]
                self.assertEqual(version_gaps, [{"kind": kind, "file": "main.js", "reason": "static_index_version"}])
                self.assertTrue(audit["metrics"]["coverage"][0]["unique_edit_window"])
                self.assertFalse(any(g.get("start_line") == 1000 for g in payload["gaps"]))
                self.assertEqual(self.state, before)

    def test_runtime_attempts_tiers_and_scalar_types_keep_identity(self):
        layers = [{"tier": "T1", "execution_ok": True, "reached": True, "values": [True], "scenario_complete": True},
                  {"tier": "T2", "execution_ok": False, "reached": None, "values": [1], "scenario_complete": False, "reason": "load_failed"}]
        for turn in (1, 2):
            self.state["reports"]["browser"]["observations"].append({"evidence_id": f"browser.t{turn}.o1",
                "tool": {"name": "reproduce", "draft": {"node_script": "run()"}, "actions": []},
                "result": {"ok": True, "node": {"ok": True}, "browser": {"ok": False},
                           "screenshot": "/not-attached.png", "witnesses": [{"id": "H1", **layers[0], "tiers": layers}]}})
        payload, audit = self.build()
        runtime = [v for v in payload["evidence"].values() if v["kind"] == "runtime_observation"]
        self.assertEqual(len(runtime), 4)
        self.assertEqual(sum(type(v["values"][0]) is bool for v in runtime), 2)
        self.assertEqual(sum(v["execution_ok"] is False for v in runtime), 2)
        self.assertEqual(reference_errors(payload), [])
        self.assertFalse(payload["evidence"]["tool:browser.t1.o1"]["runtime_image_attached_to_s5"])

    def test_minimal_recovery_is_independent_of_optional_boundaries(self):
        self.state["front_context"]["selected_boundaries"] = "malformed optional metadata"
        payload, audit = self.build()
        self.assertEqual(audit["mode"], "minimal")
        self.assertEqual(payload["mode"], "DEGRADED")
        self.assertEqual(audit["attempts"][0]["failure"]["reason"], "optional_boundary_projection_invalid")
        self.assertEqual(reference_errors(payload), [])
        self.assertEqual(payload["edit_contract"]["allowed_change"], self.state["plan"]["allowed_change"])

    def test_budget_failure_and_missing_core_are_distinct(self):
        payload, audit = self.build(budget={"max_request_text_utf8_bytes": 1})
        self.assertIsNone(payload)
        self.assertEqual(len(audit["attempts"]), 2)
        self.assertEqual(audit["failure"]["reason"], "max_request_text_utf8_bytes")
        self.state["task"]["base_commit"] = "missing"
        payload, audit = self.build()
        self.assertIsNone(payload)
        self.assertEqual(audit["failure"]["reason"], "base_identity_unavailable")

    def test_budget_degrades_without_losing_available_raw(self):
        self.raw_read()
        self.state["scope"]["bindings"]["H1"] = {"downstream_summary": {"unknown_reasons": ["unresolved " * 5000]}}
        payload, audit = self.build(budget={"max_evidence_utf8_bytes": 16384})
        self.assertIsNotNone(payload, audit)
        self.assertEqual(audit["mode"], "minimal")
        self.assertTrue(payload["evidence"]["tool:code.t1.o1"]["source_refs"])
        self.assertLessEqual(audit["metrics"]["evidence"]["utf8_bytes"], 16384)

    def test_final_prediction_certificate_keeps_program_verdict_and_scope(self):
        from causalgui.main import rank_hypotheses
        hypothesis = self.state["root"]["hypotheses"][0]
        for value, verdict in ((2, "SUPPORTED"), (3, "REFUTED")):
            with self.subTest(verdict=verdict):
                witness = {"id": "H1", "tier": "T1", "execution_ok": True, "reached": True, "values": [value],
                    "evidence": {"kind": "executed_target_write", "verified": True, "expression": "b", "target_property": "b"}}
                self.state["witnesses"] = [witness]
                self.state["root"] = rank_hypotheses([hypothesis], [witness], {"H1": {"symbol_exists": True}}, ["main.js"])
                before = deepcopy(self.state["root"])
                payload, audit = self.build()
                h = payload["hypotheses"]["items"][0]
                self.assertEqual(h["verdict"], verdict)
                self.assertFalse(h["certificate"]["excludes_candidate"])
                self.assertTrue(h["certificate"]["witness_refs"])
                self.assertEqual(self.state["root"], before)
                self.assertEqual(reference_errors(payload), [])

    def test_static_effect_location_has_original_source_and_unknown_status(self):
        self.state["scope"]["bindings"]["H1"] = {"symbol_exists": None, "interface_complete": False,
            "effect_evidence": [{"file": "effect.frag", "line": 1, "text": "untrusted copy", "kind": "property_write",
                                 "static_only": True, "target_binding_verified": False}]}
        payload, audit = self.build()
        effect = payload["evidence"]["binding:H1"]["effect_locations"][0]
        self.assertTrue(effect["source_refs"])
        self.assertNotIn("untrusted copy", json.dumps(payload))
        self.assertFalse(payload["evidence"]["binding:H1"]["interface_complete"])

    def test_no_need_to_expand_all_allowed_files_or_functions(self):
        self.state["plan"]["allowed_change"].extend([f"unexpanded/{i}.js" for i in range(200)])
        payload, audit = self.build()
        self.assertIsNotNone(payload, audit)
        self.assertEqual(payload["edit_contract"]["allowed_change"], self.state["plan"]["allowed_change"])
        self.assertEqual(len(payload["source_windows"]), 1)

    def test_exact_payload_edits_cross_files_and_creation_are_atomic(self):
        self.raw_read("style.scss")
        self.state["plan"]["must_co_edit"] = [["main.js", "style.scss", "new.js"]]
        payload, audit = self.build()
        edits = [{"file": w["file"], "search": w["text"], "replace": "// synthetic edit\n" + w["text"], "create": False}
                 for w in payload["source_windows"]]
        edits.append({"file": "new.js", "search": "", "replace": "export const fixture = 42;\n", "create": True})
        contract = payload["edit_contract"]
        result = self.apply(edits, allowed_files=contract["allowed_change"], required_groups=contract["must_co_edit"])
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["changed_files"], ["main.js", "new.js", "style.scss"])
        broken = deepcopy(edits)
        broken[1]["search"] = "not in original"
        self.assertFalse(self.apply(broken, allowed_files=contract["allowed_change"], required_groups=contract["must_co_edit"])["valid"])
        edits[-1]["file"] = "unauthorized.js"
        self.assertFalse(self.apply(edits, allowed_files=contract["allowed_change"])["valid"])

    def test_repeated_search_requires_context_present_in_payload(self):
        self.write("main.js", "function first() {\n  return 1;\n}\nfunction second() {\n  return 1;\n}")
        self.git("add", "main.js")
        self.git("commit", "-qm", "repeated")
        self.state = self.make_state(5)
        payload, audit = self.build(budget={"context_radius": 1})
        self.assertFalse(self.apply([{"file": "main.js", "search": "return 1;", "replace": "return 2;"}])["valid"])
        window = next(w for w in payload["source_windows"] if w["start_line"] <= 5 <= w["end_line"])
        self.assertTrue(self.apply([{"file": "main.js", "search": window["text"], "replace": window["text"].replace("return 1", "return 2")}])["valid"])

    def test_config_and_images_use_explicit_byte_measurement(self):
        self.state["images"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}}]
        frozen = deepcopy(self.state["images"])
        payload, audit = self.build(request_context={"messages": [{"role": "system", "content": "schema 中文"}]})
        self.assertEqual(payload["task"]["issue_images"], {"index_base": 0, "count": 1})
        self.assertEqual(audit["metrics"]["request_text"]["image_binary_bytes"], 3)
        self.assertEqual(self.state["images"], frozen)
        self.assertIsNone(audit["metrics"]["actual_input_tokens"])
        with self.assertRaises(AssertionError):
            s5_context_config({"unknown_limit": 1})

    def test_import_definition_is_bounded_and_does_not_expand_permissions(self):
        self.write("main.js", "import {helper as run} from './helper.js';\nconst b = run(2);\n")
        self.write("helper.js", "// padding\n" * 120 + "export function helper(value) {\n  return value + 1;\n}\n")
        self.git("add", "main.js", "helper.js")
        self.git("commit", "-qm", "bounded dependency")
        self.state = self.make_state()
        self.state["s0_facts"]["files"]["helper.js"] = {"source_sha256": hashlib.sha256((self.repo / "helper.js").read_bytes()).hexdigest(),
            "functions": [{"name": "helper", "line": 121, "end_line": 123}]}
        payload, audit = self.build()
        self.assertIsNotNone(payload, audit)
        self.assertTrue(any(w["file"] == "helper.js" and "return value + 1" in w["text"] for w in payload["source_windows"]))
        self.assertNotIn("helper.js", payload["edit_contract"]["allowed_change"])

    def test_static_shared_text_references_preserve_separate_evidence_meanings(self):
        symbols = ["symbol_" + str(i) for i in range(100)]
        self.state["root"]["hypotheses"][0]["readable_symbols"] = symbols
        self.state["front_context"]["selected_boundaries"] = [{"id": "B1", "file": "main.js", "line": 2, "end_line": 2,
            "repair_interface": {"G_b": "assignment", "z_b": symbols, "read_basis": "static_candidates"},
            "K_b": {}, "expressivity": {"verdict": "UNKNOWN"}}]
        before = deepcopy(self.state)
        payload, audit = self.build()
        ref = payload["hypotheses"]["items"][0]["readable_symbols_ref"]
        self.assertEqual(payload["evidence"][ref]["symbols"], symbols)
        self.assertEqual(payload["evidence"]["boundary:0"]["repair_interface"]["z_b_ref"], ref)
        self.assertEqual(payload["evidence"]["boundary:0"]["expressivity"]["verdict"], "UNKNOWN")
        self.assertEqual(reference_errors(payload), [])
        self.assertEqual(before, self.state)

    def test_dependency_gap_compaction_preserves_all_requests(self):
        requests = ["module_" + str(i) for i in range(5)]
        self.state["scope"]["unresolved_dependencies"] = [{"from": "main.js", "request": request, "reason": "unresolved_import"} for request in requests]
        payload, audit = self.build()
        groups = [gap for gap in payload["gaps"] if gap["kind"] == "upstream_scope_gaps"]
        self.assertEqual(groups[0]["requests"], requests)
        self.assertEqual(sum(g["kind"] == "upstream_scope_gap" for g in audit["attempts"][0]["gaps"]), 5)


if __name__ == "__main__":
    unittest.main()
