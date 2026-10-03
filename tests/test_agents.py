from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from causalgui.main import (AgentStep, Model, ModelOutputError, RoleSpec, BoundaryChoice, ScopePlan, Diagnosis, Candidate, agent_budget, agent_step_schema,
                           run_agent, write_json)
from causalgui.code_agent import SourceTools
from causalgui.browser_agent import BrowserTools, run_witnesses, _run


EMPTY = {"summary": "", "claims": [], "limitations": []}
SOURCE = """const view = {label: ''};
function render() {
  view.label = document.querySelector('#name').value;
  document.querySelector('#out').textContent = view.label;
}
"""
SCENE = """document.body.innerHTML='<input id="name" value="base"><button id="go">Show</button><output id="out"></output>';
document.querySelector('#go').addEventListener('click', render);
render();"""


def hypothesis():
    return {"id": "H1", "boundary_id": "output_path:main.js:3",
            "file": "main.js", "symbol": "render", "line": 3,
            "mechanism": "output_path", "question": {"kind": "value", "expression": "view.label", "expected": '"base"'},
            "readable_symbols": ["view"], "target_property": "label", "constraint_ids": []}


def source_tool(name="read_source", source_id=1):
    return {"name": name, "source_id": source_id, "query": "render", "start_line": 1, "end_line": 30}


def action(kind, target="", value=""):
    return {"kind": kind, "target": target, "value": value}


def reproduce(script=SCENE, actions=None):
    return {"name": "reproduce", "draft": {"profile": "fixture", "entry": "main.js", "node_script": "",
             "browser_script": script, "build_command": []}, "actions": actions or []}


def tools_step(*tools):
    return {"action": "tools", "tools": list(tools), "report": deepcopy(EMPTY)}


def finish(agent="code", evidence="t1.o1"):
    return {"action": "finish", "tools": [], "report": {"summary": "Observed base behavior",
            "claims": [{"hypothesis_id": "H1", "evidence_ids": [agent + "." + evidence],
                        "interpretation": "The recorded source or interaction supplies evidence.",
                        "repair_suggestion": "Review the render assignment."}], "limitations": []}}


def patch_candidate(replacement="view.label = document.querySelector('#name').value.trim();"):

    return {"rationale": "Normalize the entered value.", "edits": [{"file": "main.js",
            "search": "view.label = document.querySelector('#name').value;",
            "replace": replacement, "create": False}]}


class ScriptedModel(Model):


    def __init__(self, output, responses):
        super().__init__("scripted-no-http", 42, output, len(responses))
        self.responses, self.requests = responses, []

    def call(self, stage, system, user, schema, n=1, temperature=0, history=None):
        entry = self.reserve(stage, n)
        self.requests.append(deepcopy(history))
        write_json(self.output / f'{entry["call"]:02d}_request.json', {"stage": stage, "history": history})
        value = self.responses[self.calls - 1]
        return [json.dumps(value) if isinstance(value, dict) else value]


class FormatFeedbackContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="format-feedback-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def response(self, content, finish="stop", refusal=None):

        from openai.types.chat import ChatCompletion
        return ChatCompletion.model_validate({"id": "fixture", "object": "chat.completion", "created": 0,
            "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": content,
            "refusal": refusal}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

    def model(self, name="model"):
        return Model("fixture", 42, self.root / name, 11, format_repair_limit=1)

    def test_all_seven_schemas_recover_missing_extra_type_and_json_errors(self):
        catalog = {"protocol": "source_ids_v1", "file_count": 2, "max_id": 3}
        cases = [(RoleSpec, {"entities": ["label"], "constraints": []}),
            (BoundaryChoice, {"selections": [{"boundary_id": "fixture:1", "constraint_ids": [],
                "readable_symbols": [], "reason": "fixture", "selective": True}], "unresolved": []}),
            (ScopePlan, {"ranked_files": ["main.js"], "boundary_plans": [], "unresolved": []}),
            (Diagnosis, {"hypotheses": [hypothesis()], "new_files": []}),
            (Candidate, patch_candidate()),
            (agent_step_schema("code", catalog), tools_step(source_tool())),
            (agent_step_schema("browser", catalog), tools_step(reproduce()))]
        for schema, valid in cases:
            value = json.dumps(valid)
            schema.model_validate_json(value, strict=True)
            field = next(iter(valid))
            bad_values = [json.dumps({k: v for k, v in valid.items() if k != field}),
                          json.dumps({**valid, "undeclared": "metadata"}), json.dumps({**valid, field: 17}),
                          "```json\n" + value + "\n```", value + "\nextra explanation"]
            for index, bad in enumerate(bad_values):
                with self.subTest(schema=schema.__name__, mutation=index), patch("openai.OpenAI") as client:
                    create = client.return_value.chat.completions.create
                    create.side_effect = [self.response(bad), self.response(value)]
                    model = self.model(schema.__name__ + str(index))
                    self.assertEqual(model.call("fixture_stage", "Use fixture evidence.", "fixture", schema), [value])
                    self.assertEqual((model.calls, model.choices, len(model.physical)), (1, 1, 2))
                    self.assertEqual(create.call_count, 2)
                    initial, corrected = [call.kwargs for call in create.call_args_list]
                    self.assertEqual(corrected["messages"][:-2], initial["messages"])
                    self.assertEqual(corrected["messages"][-2]["content"], bad)
                    self.assertEqual(corrected["response_format"], initial["response_format"])
                    self.assertIn("Validation errors", corrected["messages"][-1]["content"])
                    self.assertEqual([r["status"] for r in model.physical], ["invalid_output", "completed"])

    def test_nested_types_and_coercible_values_are_rejected_before_feedback(self):
        valid = {"hypotheses": [hypothesis()], "new_files": []}
        for index, value in enumerate(["a question", {**hypothesis()["question"], "expected": True},
                                       {**hypothesis()["question"], "expression": 1}]):
            bad = deepcopy(valid)
            bad["hypotheses"][0]["question"] = value
            with self.subTest(index=index), patch("openai.OpenAI") as client:
                client.return_value.chat.completions.create.side_effect = [
                    self.response(json.dumps(bad)), self.response(json.dumps(valid))]
                model = self.model(str(index))
                model.call("S2_hypotheses", "", "", Diagnosis)
                self.assertEqual(model.physical[0]["status"], "invalid_output")
        candidate = patch_candidate()
        candidate["edits"][0]["create"] = "false"
        with patch("openai.OpenAI") as client:
            client.return_value.chat.completions.create.side_effect = [
                self.response(json.dumps(candidate)), self.response(json.dumps(patch_candidate()))]
            model = self.model("coercion")
            model.call("S5_first", "", "", Candidate)
            self.assertEqual(len(model.physical), 2)

    def test_second_invalid_output_ends_required_stage_and_keeps_raw_records(self):
        with patch("openai.OpenAI") as client:
            create = client.return_value.chat.completions.create
            create.return_value = self.response("{}")
            model = self.model()
            with self.assertRaises(ModelOutputError):
                model.call("S0_boundaries", "", "", BoundaryChoice)
            self.assertEqual(create.call_count, 2)
            self.assertEqual(model.ledger[0]["status"], "invalid_output")
            self.assertTrue((self.root / "result_data/output_contract_failure.json").exists())
            self.assertEqual(len(list(model.output.glob("*_response.json"))), 2)

    def test_candidate_failure_does_not_resample_other_candidates(self):
        value = json.dumps(patch_candidate())
        with patch("openai.OpenAI") as client:
            create = client.return_value.chat.completions.create
            create.side_effect = [self.response(v) for v in [value, "{}", "{}", value, value]]
            model = self.model()
            self.assertEqual(model.call("S5_samples", "same system", "same context", Candidate, n=4, temperature=1),
                             [value, None, value, value])
            self.assertEqual((model.calls, model.choices, create.call_count), (1, 4, 5))
            self.assertEqual([(r["sample_index"], r["correction"]) for r in model.physical],
                             [(1, 0), (2, 0), (2, 1), (3, 0), (4, 0)])
            initial = create.call_args_list[0].kwargs
            self.assertTrue(all(create.call_args_list[i].kwargs == initial for i in [1, 3, 4]))

    def test_refusal_truncation_and_transport_error_do_not_trigger_format_feedback(self):
        for finish, refusal in [("length", None), ("stop", "refused")]:
            with self.subTest(finish=finish), patch("openai.OpenAI") as client:
                create = client.return_value.chat.completions.create
                create.return_value = self.response("{}", finish, refusal)
                self.assertEqual(self.model(finish).call("S5_first", "", "", Candidate), [None])
                self.assertEqual(create.call_count, 1)
        with patch("openai.OpenAI") as client:
            create = client.return_value.chat.completions.create
            create.side_effect = RuntimeError("transport fixture")
            model = self.model("transport")
            with self.assertRaisesRegex(RuntimeError, "transport fixture"):
                model.call("S0_role_spec", "", "", RoleSpec)
            self.assertEqual(len(model.physical), 1)
            self.assertEqual(model.physical[0]["status"], "requested")


class CommandPathContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="command-path-", dir=os.getcwd())
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "work tree"
        self.repo.mkdir()

    def executable(self, path):

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nprintf "%s|%s" "$PWD" "$1"\n')
        path.chmod(0o755)
        return path

    def check_command(self, command, name):
        result = _run([*command, "argument with spaces"], self.repo, self.root / name)
        self.assertEqual(result["returncode"], 0, result)
        self.assertEqual(Path(result["stdout"]).read_text(), str(self.repo) + "|argument with spaces")

    def test_relative_executable_uses_repository_cwd(self):
        self.executable(self.repo / "tools/check")
        self.check_command(["./tools/check"], "relative")

    def test_node_bin_symlink_precedes_external_path(self):
        program = self.executable(self.repo / "tools/check")
        local = self.repo / "node_modules/.bin/cg_path_check"
        local.parent.mkdir(parents=True)
        local.symlink_to(os.path.relpath(program, local.parent))
        external = self.executable(self.root / "external/cg_path_check")
        external.write_text("#!/bin/sh\nexit 9\n")
        with patch.dict(os.environ, {"PATH": str(external.parent) + os.pathsep + os.environ["PATH"]}):
            self.check_command(["cg_path_check"], "node_bin")

    def test_absolute_and_system_commands_keep_working(self):
        program = self.executable(self.repo / "tools/check")
        self.check_command([str(program)], "absolute")
        self.check_command(["sh", str(program)], "system")

    def test_relative_path_entries_use_child_cwd(self):
        self.executable(self.repo / "tools/cg_path_check")
        with patch.dict(os.environ, {"PATH": "tools" + os.pathsep + os.environ["PATH"]}):
            self.check_command(["cg_path_check"], "relative_path")

    def test_empty_path_entry_uses_child_cwd(self):
        self.executable(self.repo / "cg_path_check")
        with patch.dict(os.environ, {"PATH": os.pathsep + os.environ["PATH"]}):
            self.check_command(["cg_path_check"], "empty_path")

    def test_missing_nonexecutable_and_directory_are_unavailable(self):
        (self.repo / "not_executable").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "directory").mkdir()
        for name in ("missing", "not_executable", "directory"):
            result = _run(["./" + name], self.repo, self.root / ("logs_" + name))
            self.assertEqual((result["returncode"], result["reason"]), (127, "executable_unavailable"))


class AgentContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="t2repair-agent-", dir=os.environ.get("CAUSALGUI_TEST_ROOT"))
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "base"
        self.repo.mkdir(parents=True, exist_ok=True)
        (self.repo / "main.js").write_text(SOURCE)
        (self.repo / "package.json").write_text('{"name":"agent-fixture","main":"main.js"}')
        environment = dict(os.environ, GIT_AUTHOR_DATE="2026-09-14T00:00:00+00:00", GIT_COMMITTER_DATE="2026-09-14T00:00:00+00:00")
        for command in (["init", "-q"], ["add", "."],
                        ["-c", "user.name=Agent fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Base fixture"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, env=environment)
        self.common = {"issue": "Show the entered name when Show is clicked.", "hypotheses": [hypothesis()],
                       "source_catalog": SourceTools(self.repo, {}).catalog()}

    def model(self, responses, name="model"):
        return ScriptedModel(self.root / name, responses)

    def test_code_tools_feed_source_and_ast_into_final_report(self):
        model = self.model([tools_step(source_tool(), source_tool("symbols"), source_tool("dependencies")), finish()])
        tools = SourceTools(self.repo, {})
        result = run_agent(model, "code", self.common, [], tools.execute, self.root / "code", 2,
                           tool_catalog=tools.catalog(), hypothesis_ids={"H1"})
        self.assertEqual(model.calls, 2)
        self.assertTrue(result["report"]["claims"][0]["citations_valid"])
        feedback = json.dumps(model.requests[1])
        self.assertIn("document.querySelector", feedback)
        self.assertIn("declarations", feedback)
        self.assertEqual(tools.read_files, ["main.js"])
        self.assertEqual((self.repo / "main.js").read_text(), SOURCE)
        forbidden = tools.execute(source_tool(source_id=-1), self.root / "forbidden")
        self.assertFalse(forbidden["ok"])

    def test_source_search_accepts_directory_without_crossing_its_scope(self):
        files = {"panel/index.js": "const needle = 1;", "panel/nested/style.scss": ".needle { color: red; }",
                 "panel-old/index.js": "const needle = 2;", "panel/tests/hidden.js": "const needle = 3;"}
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        tools = SourceTools(self.repo, {})
        for path in ("panel",):
            request = {**source_tool("search_source", tools.ids[path]), "query": "needle"}
            result = tools.execute(request, self.root / "search")
            self.assertTrue(result["ok"])
            self.assertEqual([match["file"] for match in result["matches"]], ["panel/index.js", "panel/nested/style.scss"])
            self.assertEqual(result["total_matches"], 2)
        self.assertFalse(tools.execute(source_tool("read_source", tools.ids["panel"]), self.root / "read")["ok"])
        self.assertFalse(tools.execute(source_tool("search_source", -1), self.root / "outside")["ok"])

    def test_source_ids_bind_duplicate_names_and_are_independent_of_index_order(self):
        for directory, value in (("right", 2), ("left", 1)):
            path = self.repo / directory / "shared.js"
            path.parent.mkdir()
            path.write_text(f"const value = {value};\n")
        first = SourceTools(self.repo, {"right": {"shared.js": {"text": []}}, "left": {"shared.js": {"text": []}}})
        second = SourceTools(self.repo, {"left": {"shared.js": {"text": []}}, "right": {"shared.js": {"text": []}}})
        self.assertEqual(first.catalog(), second.catalog())
        self.assertEqual(first.ids, second.ids)
        self.assertNotEqual(first.ids["left/shared.js"], first.ids["right/shared.js"])
        for directory, value in (("left", 1), ("right", 2)):
            identifier = first.ids[directory + "/shared.js"]
            output = self.root / directory
            observed = first.execute(source_tool(source_id=identifier), output)
            self.assertEqual(observed["resolved_path"], directory + "/shared.js")
            self.assertIn(f"value = {value}", observed["text"])
            self.assertEqual(json.loads((output / "source_binding.json").read_text())["source_id"], identifier)

    def test_source_registry_excludes_test_files_and_symlink_targets(self):
        hidden = self.root / "outside.js"
        hidden.write_text("const hidden = true;\n")
        (self.repo / "alias.js").symlink_to(hidden)
        (self.repo / "tests").mkdir()
        (self.repo / "tests/hidden.js").write_text("const hidden = true;\n")
        tools = SourceTools(self.repo, {})
        self.assertEqual(set(tools.ids), {"", "main.js", "package.json"})
        invalid = tools.execute(source_tool(source_id=len(tools.paths)), self.root / "invalid_id")
        self.assertEqual(invalid["error"], "unknown_source_id")
        self.assertFalse((self.root / "invalid_id").exists())

    def test_source_id_schema_rejects_paths_invalid_ids_and_directory_reads(self):
        from pydantic import ValidationError
        tools = SourceTools(self.repo, {})
        schema = agent_step_schema("code", tools.catalog())
        schema.model_validate(tools_step(source_tool()))
        schema.model_validate(tools_step(source_tool("search_source", 0)))
        invalid = [source_tool(source_id=0), source_tool(source_id=-1),
                   source_tool(source_id=len(tools.paths)), source_tool(source_id="1"),
                   {**source_tool(), "path": "invented/main.jsx"}, reproduce()]
        for tool in invalid:
            with self.subTest(tool=tool), self.assertRaises(ValidationError):
                schema.model_validate_json(json.dumps(tools_step(tool)))
        executed = []
        model = self.model([tools_step(source_tool(source_id=len(tools.paths))), finish()])
        with self.assertRaises(ValidationError):
            run_agent(model, "code", self.common, [], lambda request, output: executed.append(request), self.root / "invalid_agent", 2,
                      tool_catalog=tools.catalog(), hypothesis_ids={"H1"})
        self.assertEqual((model.calls, executed), (1, []))

    def test_empty_source_registry_allows_only_root_search(self):
        from pydantic import ValidationError
        repo = self.root / "empty"
        repo.mkdir()
        tools = SourceTools(repo, {})
        schema = agent_step_schema("code", tools.catalog())
        schema.model_validate(tools_step(source_tool("search_source", 0)))
        with self.assertRaises(ValidationError):
            schema.model_validate(tools_step(source_tool()))
        self.assertTrue(tools.execute(source_tool("search_source", 0), self.root / "empty_search")["ok"])

    def test_browser_repairs_failed_scene_and_replays_real_interactions(self):
        model = self.model([tools_step(reproduce("missingFixtureAPI();")),
                            tools_step(reproduce(actions=[action("fill", "#name", "Ada"), action("click", "#go"),
                                                          action("scroll", value="120"), action("dom", "#out")])),
                            finish("browser", "t2.o1")])
        with ExitStack() as resources:
            tools = BrowserTools(self.repo, [hypothesis()], {}, self.root / "runtime", resources)
            report = run_agent(model, "browser", self.common, [], tools.execute, self.root / "browser", 3,
                               tool_catalog=tools.sources.catalog(), hypothesis_ids={"H1"})
            draft, witnesses = tools.freeze()
        self.assertEqual(model.calls, 3)
        self.assertIn("missingFixtureAPI", json.dumps(model.requests[1]))
        self.assertIn("Ada", json.dumps(model.requests[2]))
        self.assertTrue(report["report"]["claims"][0]["citations_valid"])
        self.assertTrue(witnesses[0]["execution_ok"])
        self.assertEqual(witnesses[0]["values"][-1], "Ada")
        replay = run_witnesses(self.repo, [hypothesis()], draft, self.root / "replay")
        self.assertEqual(replay[0]["values"], witnesses[0]["values"])
        self.assertTrue(replay[0]["execution_ok"])
        self.assertEqual(model.calls, 3)
        self.assertEqual((self.repo / "main.js").read_text(), SOURCE)

    def test_failed_action_preserves_session_for_next_turn(self):
        with ExitStack() as resources:
            tools = BrowserTools(self.repo, [hypothesis()], {}, self.root / "runtime", resources)
            first = tools.execute(reproduce(actions=[action("fill", "#name", "Grace"), action("click", "#missing")]), self.root / "first")
            self.assertFalse(first["browser"]["ok"])
            session = tools.session.endpoint
            second = tools.execute({"name": "interact", "draft": None,
                                    "actions": [action("click", "#go"), action("dom", "#out")]}, self.root / "second")
            self.assertTrue(second["browser"]["ok"])
            self.assertEqual(session, tools.session.endpoint)
            self.assertIn("Grace", second["browser"]["dom"])
            draft, witnesses = tools.freeze()
        self.assertNotIn("#missing", json.dumps(draft))
        replay = run_witnesses(self.repo, [hypothesis()], draft, self.root / "replay")
        self.assertEqual(replay[0]["values"], witnesses[0]["values"])

    def test_early_finish_independence_and_unverified_claims(self):
        common = deepcopy(self.common)
        code = self.model([finish()], "code_model")
        browser = self.model([finish("browser")], "browser_model")
        tool = SourceTools(self.repo, {}).execute
        a = run_agent(code, "code", {**common, "protocol": "s2_to_code_v1"}, [], tool, self.root / "code", 2,
                      tool_catalog=common["source_catalog"], hypothesis_ids={"H1"})
        b = run_agent(browser, "browser", {**common, "protocol": "s2_to_browser_v1"}, [], tool, self.root / "browser", 3,
                      tool_catalog=common["source_catalog"], hypothesis_ids={"H1"})
        self.assertEqual((a["calls"], b["calls"]), (1, 1))
        self.assertNotEqual(code.requests[0][0], browser.requests[0][0])
        self.assertEqual(len(browser.requests[0]), 2)
        self.assertNotIn("Observed base behavior", json.dumps(browser.requests[0]))
        self.assertEqual(common, self.common)
        self.assertFalse(a["report"]["claims"][0]["citations_valid"])

    def test_last_turn_cannot_execute_tools_or_resample(self):
        model = self.model([tools_step(source_tool()), tools_step(source_tool())])
        executed = []
        def tool(request, output):
            executed.append(request)
            return {"ok": True, "file": "main.js", "start_line": 1, "end_line": 1,
                    "text": "const value = 1;\n", "truncated": False, "total_lines": 1, "sha256": "fixture"}
        report = run_agent(model, "code", self.common, [], tool, self.root / "code", 2,
                           tool_catalog=self.common["source_catalog"], hypothesis_ids={"H1"})
        self.assertEqual(report["stop_reason"], "budget_exhausted")
        self.assertEqual((model.calls, len(executed)), (2, 1))

    def test_hypothesis_limit_is_advertised_and_validated_without_truncation(self):
        from pydantic import ValidationError
        definition = Diagnosis.model_json_schema()
        self.assertEqual(definition["properties"]["hypotheses"]["maxItems"], 12)
        payload = {"hypotheses": [{**hypothesis(), "id": f"H{i}"} for i in range(1, 13)], "new_files": []}
        self.assertEqual(len(Diagnosis.model_validate_json(json.dumps(payload)).hypotheses), 12)
        payload["hypotheses"].append({**hypothesis(), "id": "H13"})
        with self.assertRaises(ValidationError):
            Diagnosis.model_validate_json(json.dumps(payload))
        self.assertEqual(len(payload["hypotheses"]), 13)

    def test_format_failure_has_durable_report_and_no_repair_call(self):
        from pydantic import ValidationError
        model = self.model(['{"action":"tools"}', finish()])
        with self.assertRaises(ValidationError):
            run_agent(model, "code", self.common, [], SourceTools(self.repo, {}).execute, self.root / "code", 2,
                      tool_catalog=self.common["source_catalog"], hypothesis_ids={"H1"})
        report = json.loads((self.root / "code/report.json").read_text())
        self.assertEqual(report["stop_reason"], "failed")
        self.assertEqual(report["error_type"], "ValidationError")
        self.assertEqual(model.calls, 1)

    def test_upstream_schemas_and_default_budgets(self):
        self.assertEqual(agent_budget({}), {"code": 2, "browser": 3, "candidates": 5})
        self.assertEqual(set(RoleSpec.model_fields), {"entities", "constraints"})
        self.assertEqual(set(ScopePlan.model_fields), {"ranked_files", "boundary_plans", "unresolved"})
        self.assertEqual(set(Diagnosis.model_fields), {"hypotheses", "new_files"})
        for schema in (RoleSpec, ScopePlan, Diagnosis, AgentStep):
            definition = schema.model_json_schema()
            for item in [definition, *definition.get("$defs", {}).values()]:
                if item.get("type") == "object":
                    self.assertFalse(item["additionalProperties"])
                    self.assertEqual(set(item["required"]), set(item["properties"]))

    def run_pipeline(self, candidates=None, case_name="case", *, progressive=False, interrupt_samples=False,
                     unavailable_agents=(), ablation=None, issue_images=()):

        from unittest.mock import patch
        from causalgui import main
        settings = {"model": "scripted-no-http"}
        if ablation is not None:
            settings["agent_ablation"] = ablation
        budget = agent_budget(settings)
        logical_limit = 6 + budget["code"] + budget["browser"]
        output = self.root / case_name / "sandbox"
        write_json(output / "preflight/result.json", {})
        candidates = [patch_candidate() for _ in range(5)] if candidates is None else candidates
        responses = [{"hypotheses": [hypothesis()], "new_files": []},
                     tools_step(source_tool()), finish(), tools_step(reproduce()),
                     tools_step({"name": "interact", "draft": None, "actions": [action("click", "#go")]}),
                     finish("browser", "t2.o1")]
        responses = dict(zip(("S2_hypotheses", "S3_code_1", "S3_code_2", "S3_browser_1", "S3_browser_2", "S3_browser_3"), responses))
        requests = []

        class PipelineModel(Model):
            def call(self, stage, system, user, schema, n=1, temperature=0, history=None):
                self.reserve(stage, n)
                body = {"n": n, "temperature": temperature, "messages": history, "stage": stage, "schema": schema.model_json_schema(), "user": user}
                if stage == "S2_hypotheses":
                    payload = json.loads(user)
                    self_outer.assertEqual(payload["protocol"], "s0_to_s2_v1")
                    self_outer.assertNotIn("front_context", payload)
                    self_outer.assertNotIn("source_context", payload)
                    self_outer.assertTrue(payload["coverage"]["required_targets_complete"])
                    self_outer.assertIn("view.label", "".join(w["text"] for w in payload["source_windows"]))
                    self_outer.assertEqual(schema, Diagnosis)
                requests.append({"request": body, "status": 200})
                if stage == "S5_samples" and interrupt_samples:
                    assert (output / "result_data/candidate_checkpoint.json").is_file()
                    raise main.ModelDeadline("S5_samples")
                if stage in {"S5_first", "S5_samples"}:
                    values = candidates[:1] if stage == "S5_first" else candidates[1:]
                    assert len(values) == n
                    return [json.dumps(value) if isinstance(value, dict) else value for value in values]
                return [json.dumps(responses[stage])] * n

            def iter_choices(self, *args, **kwargs):
                yield from self.call(*args, **kwargs)

        def seed(request, repo, output, model):

            for stage, count in (("S0_role_spec", 1), ("S0_boundaries", 1), ("S0_scope", 1)):
                model.reserve(stage, count)
                requests.append({"request": {"n": count}, "status": 200})
            model.limit = logical_limit
            write_json(output / "trajectory/S0_seed_files.json", {"protocol": "front_v1", "http_budget": logical_limit,
                       "agent_limits": budget, "topk": ["main.js"]})
            front_context = {"selected_boundaries": [{"id": "output_path:main.js:3", "file": "main.js",
                "line": 3, "end_line": 3, "expression": "view.label", "mechanism": "output_path",
                "repair_interface": {"G_b": "assignment_expression", "z_b": ["view"], "read_basis": "function_candidate_upper_bound"},
                "K_b": {"constraint_ids": [], "static_obligations": [], "declared_preserve": []},
                "expressivity": {"verdict": "UNKNOWN", "reason": "fixture"}, "selection_reason": "fixture"}],
                "scope_plan": {}, "candidate_pool": {"total": 1, "shortlist": 1},
                "value_decision_family": "output_path"}
            facts = {"files": {"main.js": {"functions": [], "source_sha256": hashlib.sha256((repo / "main.js").read_bytes()).hexdigest()}},
                     "boundary_index": {"output_path:main.js:3": {"id": "output_path:main.js:3", "file": "main.js",
                        "line": 3, "end_line": 3, "expression": "view.label", "mechanism": "output_path",
                        "edit_unit": "assignment_expression", "read_symbols": ["view"], "read_basis": "function_candidate_upper_bound"}},
                     "base_commit": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(), "boundary_unresolved": []}
            return {}, ["main.js"], self.common["issue"], list(issue_images), {"entities": [], "constraints": [],
                                                              "provenance_invalid": 0}, front_context, facts

        request = {"repo_path": str(self.repo), "output_dir": str(output),
                   "task": {"case": {"instance_id": "fixture-full", "repo": "fixture/local", "base_commit": subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip(),
                            "problem_statement": self.common["issue"], "image_assets": {"problem_statement": []}}},
                   "config": {"seed": 42, "worker": {"metadata": settings}}}
        if progressive:
            request["config"]["worker"]["metadata"]["harness_policy"] = {
                "protocol": "harness_v2", "variant": "repair", "prompt": "improved",
                "format_repair_limit": 1, "evidence": "source", "response_mode": "json_schema",
                "sample_timeout_seconds": 1800, "case_timeout_seconds": 7200,
                "finalize_reserve_seconds": 600}
        if unavailable_agents:
            request["config"]["worker"]["metadata"]["tool_feedback"] = {
                agent: {"max_observation_utf8_bytes": 1} for agent in unavailable_agents}
        self_outer = self
        with patch.object(main, "Model", PipelineModel), patch.object(main, "front_seed", seed):
            result = main.run(request)
        metrics = result["layer_metrics"]
        expected_calls = logical_limit - sum(budget[agent] - 1 for agent in unavailable_agents if budget[agent])
        self.assertEqual((metrics["candidates_generated"], metrics["model_calls"]), (1 if interrupt_samples else 5, expected_calls))
        ledger = json.loads((output / "trajectory/model_ledger.json").read_text())
        self.assertEqual(len(ledger), len(requests))
        self.assertEqual(len(ledger), expected_calls)
        self.assertEqual(metrics["model_call_budget"], logical_limit)
        self.assertEqual([row["n"] for row in ledger], [row["request"]["n"] for row in requests])
        stages = ["S0_role_spec", "S0_boundaries", "S0_scope", "S2_hypotheses"]
        for agent in ("code", "browser"):
            report = json.loads((output / f"trajectory/agents/{agent}/report.json").read_text())
            self.assertLessEqual(report["calls"], budget[agent])
            stages.extend(f"S3_{agent}_{turn}" for turn in range(1, report["calls"] + 1))
        self.assertEqual([row["stage"] for row in ledger], stages + ["S5_first", "S5_samples"])
        self.assertEqual([row["n"] for row in ledger][-2:], [1, 4])
        self.assertEqual([row["request"]["temperature"] for row in requests[-2:]], [0, 1])
        self.assertEqual(requests[-2]["request"]["user"], requests[-1]["request"]["user"])
        self.assertEqual(json.loads(requests[-1]["request"]["user"][0]["text"])["protocol"], "s5_context_v1")
        self.assertEqual(metrics["candidates_requested"], 5)
        self.assertEqual(metrics["model_choices"], expected_calls + 3)
        self.assertEqual(metrics["status"], "partial" if interrupt_samples else "completed")
        self.assertEqual(json.loads((output / "case_result.json").read_text()), result)
        self.assertEqual(json.loads((output / "result_data/layer_metrics.json").read_text()), metrics)
        self.assertEqual((output / "patch.diff").read_bytes(), (output / "patch/final.patch").read_bytes())
        self.assertEqual(bool((output / "patch.diff").read_text()), result["selected_candidate"] is not None)
        inputs = {}
        for agent in ("code", "browser"):
            messages = [r["request"]["messages"][0] for r in requests if r["request"].get("stage") == f"S3_{agent}_1"]
            self.assertEqual(len(messages), int(bool(budget[agent])))
            if messages:
                inputs[agent] = json.loads(messages[0]["content"][0]["text"])
                self.assertEqual(inputs[agent]["protocol"], f"s2_to_{agent}_v1")
                self.assertEqual(messages[0]["content"][1:], list(issue_images))
        if len(inputs) == 2:
            self.assertNotEqual(inputs["code"], inputs["browser"])
            for key in ("task", "requirements", "hypotheses"):
                self.assertEqual(inputs["code"][key], inputs["browser"][key])
        if "browser" in inputs:
            self.assertNotIn("tool_observations", json.dumps(inputs["browser"]))
        self.assertEqual(requests[-1]["request"]["user"][1:], list(issue_images))
        self.assertEqual((self.repo / "main.js").read_text(), SOURCE)
        self.assertFalse(subprocess.check_output(["git", "-C", str(self.repo), "diff", "HEAD", "--"]))
        return result, output, requests

    def test_agent_ablation_removes_execution_context_and_replay_but_keeps_repair(self):
        from causalgui import browser_agent, code_agent

        images = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="}}]
        for variant, disabled, expected_calls in (("without-code", "code", 9), ("without-browser", "browser", 8)):
            for progressive in (False, True):
                with self.subTest(variant=variant, progressive=progressive), ExitStack() as guards:
                    if disabled == "code":
                        guards.enter_context(patch.object(code_agent, "build_code_agent_payload",
                            side_effect=AssertionError("Disabled Code Agent constructed input")))
                    else:
                        guards.enter_context(patch.object(browser_agent, "BrowserTools",
                            side_effect=AssertionError("Disabled Browser Agent created tools")))
                        guards.enter_context(patch.object(browser_agent, "run_witnesses",
                            side_effect=AssertionError("Disabled Browser Agent replayed witnesses")))
                    result, output, requests = self.run_pipeline(
                        case_name=f"{variant}_{progressive}", ablation=variant, progressive=progressive, issue_images=images)
                self.assertEqual(result["layer_metrics"]["model_calls"], expected_calls)
                self.assertEqual(result["layer_metrics"][disabled + "_agent_calls"], 0)
                self.assertEqual(result["layer_metrics"]["agent_ablation"], variant)
                self.assertIsNotNone(result["selected_candidate"])
                state = json.loads((output / "trajectory/S5_input_state.json").read_text())
                self.assertNotIn(disabled, state["initial_payloads"])
                self.assertNotIn(disabled, state["agent_audits"])
                self.assertEqual(state["reports"][disabled]["observations"], [])
                self.assertFalse((output / f"trajectory/agents/{disabled}/input_payload.json").exists())
                payload = json.loads(requests[-1]["request"]["user"][0]["text"])
                self.assertEqual(payload["agents"][disabled]["stop_reason"], "disabled_by_ablation")
                self.assertEqual(payload["agents"][disabled]["evidence_refs"], [])
                self.assertEqual(payload["agents"][disabled]["claims"], [])
                if disabled == "browser":
                    self.assertEqual(state["witnesses"], [])
                    self.assertFalse((output / "trajectory/S3b").exists())
                    validation = json.loads((output / "trajectory/candidates/01/validation.json").read_text())
                    self.assertTrue(validation["patch_applied"])
                    self.assertTrue(validation["syntax_ok"])
                    self.assertIn("regression_status", validation)
                    self.assertEqual(validation["witnesses"], [])
                    self.assertEqual(validation["render_artifacts"], [])
                    self.assertFalse(validation["probe_preserved"])
                    self.assertFalse((output / "trajectory/candidates/01/validation/replay").exists())

    def test_explicit_full_matches_default_pipeline(self):
        first, _, first_requests = self.run_pipeline(case_name="default_full")
        second, _, second_requests = self.run_pipeline(case_name="explicit_full", ablation="full")
        self.assertEqual(first["selected_candidate"], second["selected_candidate"])
        self.assertEqual(first["layer_metrics"], second["layer_metrics"])
        for requests in (first_requests, second_requests):
            self.assertEqual([r["request"].get("stage") for r in requests[3:]], [
                "S2_hypotheses", "S3_code_1", "S3_code_2", "S3_browser_1", "S3_browser_2", "S3_browser_3", "S5_first", "S5_samples"])

    def test_progressive_pipeline_saves_before_later_model_deadline(self):
        result, output, _ = self.run_pipeline(progressive=True, interrupt_samples=True)
        self.assertEqual(result["selected_candidate"], 1)
        checkpoint = json.loads((output / "result_data/candidate_checkpoint.json").read_text())
        self.assertEqual(checkpoint["selected"]["patch"], (output / "patch.diff").read_text())
        self.assertFalse(result["layer_metrics"]["generation_complete"])
        self.assertEqual(result["layer_metrics"]["termination"]["reason"], "ModelDeadline")

    def test_progressive_complete_pipeline_keeps_original_selection(self):
        result, output, _ = self.run_pipeline(progressive=True)
        self.assertEqual(result["selected_candidate"], 1)
        self.assertTrue(result["layer_metrics"]["generation_complete"])
        self.assertEqual(json.loads((output / "result_data/candidate_checkpoint.json").read_text())["selected"]["index"], 1)

    def test_full_pipeline_five_candidates_and_audited_stage_order(self):
        result, output, requests = self.run_pipeline()
        metrics = result["layer_metrics"]
        self.assertEqual(metrics["unique_candidates_validated"], 1)
        self.assertEqual(metrics["candidates_invalid_output"], 0)
        applications = [json.loads(path.read_text()) for path in sorted((output / "trajectory/candidates").glob("*/application.json"))]
        self.assertEqual(len(applications), 5)
        self.assertTrue(all(row["patch_protocol"] == "exact_unique_search_v1" and row["valid"] for row in applications))
        self.assertTrue(all(row["locations"][0]["match_count"] == 1 for row in applications))
        for request in requests[-2:]:
            schema = request["request"]["schema"]
            self.assertEqual(set(schema["$defs"]["Edit"]["properties"]), {"file", "search", "replace", "create"})

    def test_feedback_unavailable_agents_do_not_prevent_other_agent_or_s5(self):
        for unavailable in (("code",), ("browser",), ("code", "browser")):
            with self.subTest(unavailable=unavailable):
                result, output, requests = self.run_pipeline(case_name="unavailable_" + "_".join(unavailable), unavailable_agents=unavailable)
                synthesis = json.loads(requests[-1]["request"]["user"][0]["text"])
                self.assertEqual(result["layer_metrics"]["candidates_generated"], 5)
                for agent in ("code", "browser"):
                    report = synthesis["agents"][agent]
                    self.assertEqual(report["stop_reason"], "feedback_unavailable" if agent in unavailable else "completed")
                    self.assertTrue(report["evidence_refs"])
                    if agent in unavailable:
                        self.assertEqual(report["claims"], [])
                        self.assertIn("Controller status", report["summary"])
                        self.assertFalse(report["model_report_available"])
                self.assertNotIn("feedback_audit", synthesis)
                self.assertTrue((output / "trajectory/S3b/replay_plan.json").is_file())

    def test_fixed_tools_and_reports_leave_s5_evidence_unchanged(self):
        from causalgui import feedback
        _, output, first = self.run_pipeline(case_name="projected")
        baseline = json.loads(first[-1]["request"]["user"][0]["text"])
        reports = json.loads((output / "trajectory/S5_input_state.json").read_text())["reports"]
        draft = json.loads((output / "trajectory/probe_draft.json").read_text())
        witnesses = json.loads((output / "trajectory/S3b_witnesses.json").read_text())
        construct = feedback.construct_feedback
        def original_feedback(agent, observations, *args, **kwargs):
            view, audit = construct(agent, observations, *args, **kwargs)
            self.assertIsNotNone(view)
            view[0]["text"] = json.dumps({"tool_observations": observations}, ensure_ascii=False)
            return view, audit
        with patch.object(SourceTools, "execute", side_effect=[deepcopy(o["result"]) for o in reports["code"]["observations"]]), \
             patch.object(BrowserTools, "execute", side_effect=[deepcopy(o["result"]) for o in reports["browser"]["observations"]]), \
             patch.object(BrowserTools, "freeze", return_value=(draft, witnesses)), \
             patch.object(feedback, "construct_feedback", side_effect=original_feedback):
            _, _, second = self.run_pipeline(case_name="original_feedback")
        comparison = json.loads(second[-1]["request"]["user"][0]["text"])
        self.assertEqual(comparison["agents"], baseline["agents"])
        self.assertEqual(comparison["hypotheses"], baseline["hypotheses"])
        self.assertEqual(comparison["edit_contract"], baseline["edit_contract"])
        self.assertEqual(comparison["evidence"], baseline["evidence"])

    def test_invalid_candidate_outputs_preserve_other_candidates(self):
        from unittest.mock import patch
        from causalgui import synthesis

        valid, missing = patch_candidate(), patch_candidate()
        del missing["edits"][0]["create"]
        for name, candidates, selected, accepted in (
            ("bad_second_and_fourth", [valid, missing, valid, missing, valid], 1, [1, 3, 5]),
            ("bad_first_and_truncated", [missing, valid, missing, None, valid], 2, [2, 5]),
        ):
            with self.subTest(name=name), patch.object(synthesis, "apply_candidate", wraps=synthesis.apply_candidate) as apply:
                result, output, _ = self.run_pipeline(candidates, name)
            metrics = result["layer_metrics"]
            self.assertEqual(apply.call_count, len(accepted))
            self.assertEqual(result["selected_candidate"], selected)
            self.assertEqual(metrics["candidates_invalid_output"], 2)
            self.assertEqual(metrics["candidates_atomic_valid"], len(accepted))
            self.assertEqual(metrics["candidates_validated"], len(accepted))
            self.assertEqual(metrics["unique_candidates_validated"], 1)
            selection = json.loads((output / "trajectory/S6_selection.json").read_text())
            self.assertEqual([row["index"] for row in selection["ranking"]], accepted)
            self.assertEqual(selection["first_nonempty"], selected)
            folders = output / "trajectory/candidates"
            self.assertEqual(sorted(path.name for path in folders.iterdir()), ["01", "02", "03", "04", "05"])
            self.assertEqual((output / "patch.diff").read_bytes(), (folders / f"{selected:02d}/candidate.patch").read_bytes())
            for index, value in enumerate(candidates, 1):
                folder = folders / f"{index:02d}"
                if value is None:
                    self.assertEqual(json.loads((folder / "result.json").read_text())["reason"], "truncated_or_refused")
                    self.assertFalse((folder / "candidate.patch").exists())
                elif index not in accepted:
                    record = json.loads((folder / "application.json").read_text())
                    self.assertEqual(record["reason"], "invalid_candidate_output")
                    self.assertEqual(record["errors"][0]["loc"], ["edits", 0, "create"])
                    self.assertEqual(record["errors"][0]["type"], "missing")
                    self.assertEqual((folder / "raw_response.txt").read_text(), json.dumps(value))
                    self.assertFalse((folder / "candidate.patch").exists())
                elif index != selected:
                    validation = json.loads((folder / "validation.json").read_text())
                    self.assertEqual(validation["reused_from_candidate"], selected)

    def test_all_invalid_outputs_finish_without_partial_patches(self):
        from unittest.mock import patch
        from causalgui import browser_agent, synthesis

        missing, forbidden, atomic = patch_candidate(), patch_candidate(), patch_candidate()
        del missing["edits"][0]["create"]
        forbidden["edits"][0]["start_line"] = None
        atomic["edits"].append(deepcopy(missing["edits"][0]))
        candidates = ['{"rationale":', missing, {"rationale": "Bad type", "edits": "not a list"}, forbidden, atomic]
        errors = [("json_invalid", []), ("missing", ["edits", 0, "create"]),
                  ("list_type", ["edits"]), ("extra_forbidden", ["edits", 0, "start_line"]),
                  ("missing", ["edits", 1, "create"])]
        with patch.object(synthesis, "apply_candidate", side_effect=AssertionError("Invalid output reached application")) as apply, \
             patch.object(browser_agent, "validate_patch", side_effect=AssertionError("Invalid output reached validation")) as validate:
            result, output, _ = self.run_pipeline(candidates)
        apply.assert_not_called()
        validate.assert_not_called()
        self.assertIsNone(result["selected_candidate"])
        self.assertEqual(result["layer_metrics"]["candidates_invalid_output"], 5)
        for counter in ("candidates_atomic_valid", "candidates_validated", "unique_candidates_validated"):
            self.assertEqual(result["layer_metrics"][counter], 0)
        selection = json.loads((output / "trajectory/S6_selection.json").read_text())
        self.assertEqual(selection["ranking"], [])
        self.assertIsNone(selection["first_nonempty"])
        for index, (value, (kind, location)) in enumerate(zip(candidates, errors), 1):
            folder = output / f"trajectory/candidates/{index:02d}"
            record = json.loads((folder / "application.json").read_text())
            self.assertFalse(record["valid"])
            self.assertEqual(record["reason"], "invalid_candidate_output")
            self.assertEqual((record["patch"], record["changed_files"], record["locations"]), ("", [], []))
            self.assertEqual(record["patch_protocol"], "exact_unique_search_v1")
            self.assertEqual((record["errors"][0]["type"], record["errors"][0]["loc"]), (kind, location))
            raw = json.dumps(value) if isinstance(value, dict) else value
            self.assertEqual((folder / "raw_response.txt").read_bytes(), raw.encode("utf-8"))
            self.assertFalse((folder / "candidate.patch").exists())
            self.assertFalse((folder / "candidate.json").exists())

    def test_later_valid_candidate_keeps_existing_ranking(self):
        from unittest.mock import patch
        from causalgui import browser_agent

        first = patch_candidate()
        later = patch_candidate("view.label = document.querySelector('#name').value.toUpperCase();")
        with patch.object(browser_agent, "validate_patch", side_effect=[
            {"patch_applied": True, "syntax_ok": None}, {"patch_applied": True, "syntax_ok": True},
        ]) as validate:
            result, output, _ = self.run_pipeline([first, "{}", later, first, later])
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(result["selected_candidate"], 3)
        self.assertEqual(result["layer_metrics"]["selection_rank_changed_vs_first_nonempty"], 1)
        self.assertEqual(result["layer_metrics"]["unique_candidates_validated"], 2)
        selection = json.loads((output / "trajectory/S6_selection.json").read_text())
        self.assertEqual(selection["first_nonempty"], 1)
        self.assertEqual([row["index"] for row in selection["ranking"]], [3, 5, 1, 4])
        self.assertEqual((output / "patch.diff").read_bytes(), (output / "trajectory/candidates/03/candidate.patch").read_bytes())

    def test_candidate_tool_errors_are_not_output_errors(self):
        from pydantic import ValidationError
        from unittest.mock import patch
        from causalgui import synthesis

        errors = [RuntimeError("Fixture tool failure"),
                  ValidationError.from_exception_data("ToolResult", [{"type": "missing", "loc": ("result",), "input": {}}])]
        for error in errors:
            name = type(error).__name__
            with self.subTest(error=name), patch.object(synthesis, "apply_candidate", side_effect=error), \
                 self.assertRaises(type(error)) as raised:
                self.run_pipeline(case_name=name)
            self.assertIs(raised.exception, error)
            output = self.root / name / "sandbox"
            metrics = json.loads((output / "result_data/layer_metrics.json").read_text())
            self.assertEqual((metrics["candidates_generated"], metrics["candidates_invalid_output"]), (1, 0))
            self.assertFalse((output / "patch.diff").exists())
            self.assertFalse((output / "trajectory/candidates/01/application.json").exists())


class ToolFeedbackContracts(unittest.TestCase):
    def setUp(self):
        from causalgui import feedback
        self.module = feedback
        self.history = [{"role": "user", "content": [{"type": "text", "text": "{}"}]}]
        self.text = "const value = 'actual base source';\r\n" * 40
        self.observation = {"evidence_id": "code.t1.o1", "tool": source_tool(), "result": {
            "ok": True, "file": "main.js", "source_id": 1, "sha256": "version-one",
            "start_line": 1, "end_line": 40, "total_lines": 80, "text": self.text, "truncated": True}}

    def build(self, observations=None, history=None, agent="code", mode="normal", budget=None, **kwargs):
        history = self.history if history is None else history
        return self.module.build_agent_feedback(agent, observations or [self.observation],
            self.module.visible_context_index(agent, history), 1, budget,
            request_context={"messages": [{"role": "system", "content": "SYSTEM SCHEMA"}, *history],
                             "response_format": {"schema": "OUTPUT SCHEMA"}, "next_turn": {"role": "user", "content": "FINAL"}},
            mode=mode, **kwargs)

    def test_raw_and_mutable_children_are_independent_and_output_is_stable(self):
        original = deepcopy(self.observation)
        first, audit = self.build()
        self.assertEqual((first, audit), self.build())
        self.assertEqual(self.observation, original)
        self.assertEqual(audit["raw_sha256"], self.module.digest([original]))
        projected = json.loads(first[0]["text"])
        projected["tool_observations"][0]["result"]["text"] = "changed"
        self.assertEqual(self.observation, original)
        self.assertTrue(audit["observations"][0]["required_facts_preserved"])

    def test_same_agent_visible_original_source_is_referenced(self):
        payload = {"source_windows": [{"id": "W1", "file": "main.js", "source_sha256": "version-one",
                    "start_line": 1, "end_line": 40, "text": self.text}]}
        history = [{"role": "user", "content": [{"type": "text", "text": json.dumps(payload)}]}]
        feedback, audit = self.build(history=history)
        self.assertTrue(audit["observations"][0]["source_reference"])
        ref = json.loads(feedback[0]["text"])["tool_observations"][0]["result"]["text"]["feedback_content_ref"]
        self.assertEqual(ref["origin"], "initial_context")
        self.assertEqual(self.module.resolve_reference(ref, self.module.visible_context_index("code", history)), (True, self.text))
        self.assertFalse(self.module.resolve_reference(ref, self.module.visible_context_index("browser", history))[0])
        self.observation["result"]["sha256"] = "other-version"
        self.assertFalse(self.build(history=history)[1]["observations"][0]["source_reference"])

    def test_repeat_source_can_resolve_prior_tool_feedback(self):
        first, _ = self.build()
        history = self.history + [{"role": "user", "content": first}]
        self.observation["evidence_id"] = "code.t2.o1"
        second, audit = self.build(history=history)
        self.assertTrue(audit["observations"][0]["source_reference"])
        self.assertIn("code.t2.o1", second[0]["text"])

    def test_complete_subrange_resolves_but_partial_line_does_not(self):
        payload = {"source_windows": [{"id": "W1", "file": "main.js", "source_sha256": "version-one",
                    "start_line": 1, "end_line": 40, "text": self.text}]}
        history = [{"role": "user", "content": [{"type": "text", "text": json.dumps(payload)}]}]
        self.observation["result"].update(start_line=3, end_line=25, text="".join(self.text.splitlines(keepends=True)[2:25]))
        self.assertTrue(self.build(history=history)[1]["observations"][0]["source_reference"])
        self.observation["result"]["text"] = self.observation["result"]["text"][:-5]
        self.assertFalse(self.build(history=history)[1]["observations"][0]["source_reference"])

    def test_actual_source_range_does_not_trust_declared_end(self):
        result = {**self.observation["result"], "text": "x" * 23999 + "\r", "end_line": 240, "total_lines": 900}
        coverage = self.module.source_visibility(source_tool(), result)
        self.assertEqual(coverage["raw_text_range"], [1, 1])
        self.assertEqual(coverage["last_line"], "unknown")
        self.assertNotEqual(coverage["status"], "complete")
        result["text"] = "x" * 24000
        self.assertEqual(self.module.source_visibility(source_tool(), result)["last_line"], "partial")
        result.update(end_line=1, total_lines=1)
        self.assertEqual(self.module.source_visibility(source_tool(), result)["status"], "unknown")
        result["truncated"] = False
        self.assertEqual(self.module.source_visibility(source_tool(), result)["status"], "complete")

    def test_no_trimming_crlf_unicode_or_eof_text(self):
        for text in ("中文\r\n  end", "a\n", "最后一行"):
            self.observation["result"].update(text=text, end_line=len(text.splitlines()), truncated=False)
            feedback, _ = self.build()
            self.assertEqual(json.loads(feedback[0]["text"])["tool_observations"][0]["result"]["text"], text)

    def test_search_and_listing_pages_are_complete_and_keep_ids(self):
        for name, field, count in (("search_source", "matches", 60), ("list_sources", "files", 200)):
            rows = [{"source_id": i + 301, "file": f"src/{i}.js", **({"line": i + 1, "text": "hit"} if name == "search_source" else {})}
                    for i in range(count)]
            original = {"evidence_id": "code.t1.o1", "tool": {**source_tool(name, 0), "start_line": count},
                        "result": {"ok": True, field: rows, "next_offset": count * 2, "total": count * 3}}
            feedback, _ = self.build([original])
            shown = json.loads(feedback[0]["text"])["tool_observations"][0]
            self.assertEqual(shown["result"], original["result"])
            self.assertEqual(shown["tool"], original["tool"])

    def test_new_relations_symbols_and_unknown_fields_are_not_dropped(self):
        for name, result in (("dependencies", {"neighbor_sources": [{"source_id": 999, "file": "outside.js", "relation": "consumer"}],
                                              "unresolved": [{"request": "unknown", "reason": "unresolved_import"}]}),
                             ("symbols", {"declarations": ["unexpectedCaller"], "members": ["counterexample"], "probes": []})):
            observation = {"evidence_id": "code.t1.o1", "tool": source_tool(name),
                           "result": {"ok": True, **result, "new_fact": "must retain until reviewed"}}
            feedback, audit = self.build([observation])
            self.assertEqual(json.loads(feedback[0]["text"])["tool_observations"][0]["result"], observation["result"])
            self.assertEqual(audit["pending_validation"][0]["policy"], "retained")

    def browser_observation(self, identity="browser.t1.o1", name="reproduce"):
        return {"evidence_id": identity, "tool": reproduce() if name == "reproduce" else {"name": "interact", "draft": None, "actions": []},
                "result": {"ok": True, "node": {"ok": True, "state": {"loaded": ["main.js"], "scenario_complete": True}},
                    "browser": {"ok": False, "completed_actions": [action("open")], "actual_scene": False,
                        "state": {"scenario_complete": False, "loaded": [], "errors": ["counterexample"],
                                  "events": [{"id": "H1", "value": 0, "read_succeeded": False}]},
                        "stderr": "selector failure", "html": "DEFAULT HTML" * 100,
                        "query": [{"text": "directed fact", "html": "DIRECTED HTML"}]},
                    "witnesses": [{"id": "H1", "tier": "T2", "reached": None, "execution_ok": False,
                        "values": [0], "coverage": {"complete": False}, "tiers": [{"tier": "T2", "reached": None,
                        "values": [0], "execution_ok": False, "scenario_complete": False}]}]}}

    def test_node_success_browser_failure_and_directed_dom_are_preserved(self):
        observation = self.browser_observation()
        feedback, audit = self.build([observation], agent="browser")
        shown = json.loads(feedback[0]["text"])["tool_observations"][0]
        self.assertTrue(shown["result"]["ok"])
        self.assertFalse(shown["result"]["browser"]["ok"])
        self.assertEqual(shown["result"]["browser"]["state"], observation["result"]["browser"]["state"])
        self.assertEqual(shown["result"]["browser"]["query"], observation["result"]["browser"]["query"])
        self.assertEqual(shown["failed_action"], "unknown")
        self.assertEqual(audit["omissions"][0]["reason"], "default_html_not_directed_dom_query")

    def test_attempts_and_equal_event_values_are_never_merged(self):
        first, _ = self.build([self.browser_observation()], agent="browser")
        history = self.history + [{"role": "user", "content": first}]
        for name, expected in (("interact", "browser.t1.o1"), ("reproduce", "browser.t2.o1")):
            observation = self.browser_observation("browser.t2.o1", name)
            feedback, _ = self.build([observation], history=history, agent="browser")
            shown = json.loads(feedback[0]["text"])["tool_observations"][0]
            self.assertEqual(shown["scene_ref"], expected)
            self.assertEqual(shown["result"]["browser"]["state"]["events"], observation["result"]["browser"]["state"]["events"])

    def test_boolean_and_number_runtime_values_remain_distinct(self):
        observation = self.browser_observation()
        witness = observation["result"]["witnesses"][0]
        witness["values"] = [False] * 200
        witness["tiers"][0]["values"] = [0] * 200
        feedback, audit = self.build([observation], agent="browser")
        shown = json.loads(feedback[0]["text"])["tool_observations"][0]["result"]["witnesses"][0]
        self.assertIs(shown["values"][0], False)
        self.assertIs(type(shown["tiers"][0]["values"][0]), int)
        self.assertEqual(audit["observations"][0]["runtime_aliases"], 0)

    def test_completed_failed_test_does_not_become_pass(self):
        result = {"ok": True, "status": "FAIL", "summary": {"complete": True, "total": 2, "passed": 1, "failed": 1},
                  "process": {"returncode": 1, "command": ["npm", "test"]}, "output_tail": "expected blue, received red"}
        observation = {"evidence_id": "code.t1.o1", "tool": source_tool("run_tests"), "result": result}
        for mode in ("normal", "emergency"):
            feedback, _ = self.build([observation], mode=mode)
            self.assertEqual(json.loads(feedback[0]["text"])["tool_observations"][0]["result"], result)

    def test_budget_covers_existing_messages_schema_and_images_separately(self):
        picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}
        feedback, audit = self.build(image_parts=[picture], image_bindings=[{"evidence_id": "code.t1.o1", "attachment_index": 0}])
        self.assertEqual(feedback[1], picture)
        self.assertEqual(audit["metrics"]["next_request"]["image_binary_bytes"], 1)
        for key in self.module.DEFAULT_BUDGET:
            failed, result = self.build(budget={key: 1})
            self.assertIsNone(failed)
            self.assertIn("budget", result["failure"]["reason"])
        huge = self.history + [{"role": "assistant", "content": "long parameter " * 10000}]
        failed, audit = self.build(history=huge, budget={"max_request_text_utf8_bytes": 30000})
        self.assertIsNone(failed)
        self.assertEqual(audit["failure"]["reason"], "required_request_exceeds_budget")

    def test_emergency_keeps_evidence_and_can_remove_only_artifact_overhead(self):
        observation = {"evidence_id": "code.t1.o1", "tool": source_tool("symbols"),
            "result": {"ok": False, "reason": "ast_parse_failed", "probes": [],
                       "process": {"returncode": 1, "command": ["node", "ast.cjs"], "stderr": "/audit/" + "long" * 1000}}}
        feedback, audit = self.module.construct_feedback("code", [observation], self.module.visible_context_index("code", self.history),
            1, {"max_observation_utf8_bytes": 1500}, request_context={"messages": self.history})
        self.assertIsNotNone(feedback)
        self.assertEqual(audit["status"], "degraded")
        shown = json.loads(feedback[0]["text"])["tool_observations"][0]["result"]
        self.assertEqual(shown["reason"], "ast_parse_failed")
        self.assertEqual(shown["process"]["returncode"], 1)
        self.assertEqual(len(audit["attempts"]), 2)

    def test_projection_exception_isolated_from_tools_and_emergency(self):
        original = self.module.build_agent_feedback
        def failure(*args, **kwargs):
            if kwargs["mode"] == "normal":
                raise ValueError("normal projection fixture")
            return original(*args, **kwargs)
        with patch.object(self.module, "build_agent_feedback", side_effect=failure):
            result, audit = self.module.construct_feedback("code", [self.observation],
                self.module.visible_context_index("code", self.history), 1, None, request_context={"messages": self.history})
        self.assertIsNotNone(result)
        self.assertEqual(audit["status"], "degraded")
        self.assertEqual(audit["attempts"][0]["failure"]["reason"], "projection_exception")

    def test_controller_raw_history_unavailability_and_other_agent(self):
        with tempfile.TemporaryDirectory(prefix="feedback-controller-") as tmp:
            root = Path(tmp)
            catalog = {"protocol": "source_ids_v1", "file_count": 1, "max_id": 1}
            reports = []
            for budget, name in ((None, "normal"), ({"max_observation_utf8_bytes": 1}, "unavailable")):
                called = []
                def execute(tool, folder):
                    called.append(deepcopy(tool))
                    return deepcopy(self.observation["result"])
                model = ScriptedModel(root / name / "model", [tools_step(source_tool()), finish()])
                result = run_agent(model, "code", {}, [], execute, root / name, 2,
                    tool_catalog=catalog, hypothesis_ids={"H1"}, feedback_budget=budget)
                reports.append(result)
                self.assertEqual(called, [source_tool()])
                self.assertEqual(result["observations"], [self.observation])
                self.assertEqual(json.loads((root / name / "turn_01/tool_01/observation.json").read_text()), self.observation)
                if budget is None:
                    feedback = json.loads((root / name / "turn_01/feedback.json").read_text())
                    self.assertEqual(model.requests[1][-2]["content"], feedback)
                    self.assertTrue(result["report"]["claims"][0]["citations_valid"])
                else:
                    self.assertEqual(result["stop_reason"], "feedback_unavailable")
                    self.assertEqual(model.calls, 1)
                    self.assertFalse((root / name / "turn_01/feedback.json").exists())
                    self.assertEqual(result["report"]["claims"], [])
            browser = ScriptedModel(root / "browser_model", [finish("browser")])
            other = run_agent(browser, "browser", {}, [], lambda *args: self.fail("no tool expected"), root / "browser", 3,
                              tool_catalog=catalog, hypothesis_ids={"H1"})
            self.assertEqual(other["stop_reason"], "completed")
            self.assertNotIn("code.t1.o1", json.dumps(browser.requests))
            self.assertEqual(reports[0]["observations"], reports[1]["observations"])

    def test_invalid_budget_configuration_is_rejected(self):
        for config in ({"code": {"unknown": 1}}, {"browser": {"max_feedback_utf8_bytes": True}}, {"protocol": "wrong"}):
            with self.assertRaises(AssertionError):
                self.module.feedback_config(config)

    def test_offline_manifest_keeps_missing_no_tool_and_failure_cases(self):
        import replay_tool_feedback as replay
        with tempfile.TemporaryDirectory(prefix="feedback-replay-") as tmp:
            root = Path(tmp)
            batch, inputs, output = root / "batch", root / "inputs", root / "output"
            write_json(batch / "selected_cases.json", {"instance_ids": ["available", "missing"]})
            write_json(batch / "run_manifest.json", {"config": {"worker": {"metadata": {"agent_limits": {"code": 2, "browser": 3}}}}})
            trajectory = batch / "cases/available/trajectory"
            history = [*self.history, {"role": "user", "content": "{}"},
                {"role": "assistant", "content": json.dumps(tools_step(source_tool()))}]
            parts = [{"type": "text", "text": json.dumps({"tool_observations": [self.observation]})}]
            history += [{"role": "user", "content": parts}, {"role": "user", "content": "{}"}]
            body = {"model": "frozen", "n": 1, "temperature": 0, "seed": 42,
                    "messages": [{"role": "system", "content": "frozen system"}, *history]}
            write_json(trajectory / "05_S3_code_2_request.json", body)
            write_json(trajectory / "agents/code/turn_01/tool_01/observation.json", self.observation)
            write_json(trajectory / "agents/code/history.json", history + [{"role": "assistant", "content": "FUTURE_SENTINEL"}])
            write_json(trajectory / "agents/browser/history.json", self.history)
            write_json(batch / "gold.json", {"must_not_read": "GOLD_SENTINEL"})
            manifest = replay.prepare(batch, inputs)
            summary = replay.replay(inputs, output, self.module.feedback_config())
            self.assertEqual((summary["planned_cases"], summary["planned_agents"]), (2, 4))
            self.assertEqual(summary["statuses"]["code:upstream_history_unavailable"], 1)
            self.assertEqual(summary["statuses"]["browser:no_tool_calls"], 1)
            self.assertEqual((summary["model_calls"], summary["tool_calls"]), (0, 0))
            import gzip
            frozen = gzip.decompress((inputs / "available.json.gz").read_bytes()).decode()
            self.assertNotIn("FUTURE_SENTINEL", frozen)
            self.assertNotIn("GOLD_SENTINEL", frozen)
            failed = replay.replay(inputs, root / "failed", self.module.feedback_config({"code": {"max_observation_utf8_bytes": 1}}))
            self.assertEqual(failed["statuses"]["code:feedback_unavailable"], 1)
            replay.prepare_pairs(inputs, output, root / "pairs")
            paired = json.loads((root / "pairs/manifest.json").read_text())
            self.assertEqual((paired["prepared_pairs"], paired["model_calls"]), (1, 0))
            self.assertEqual(len(manifest["cases"]), 2)
