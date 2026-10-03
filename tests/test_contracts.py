from copy import deepcopy
import hashlib
import json
import subprocess
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from causalgui.code_agent import (build_scope, build_hypothesis_payload, s2_context_config, SourceTools,
                                  build_code_agent_payload, build_browser_agent_payload, agent_context_config)
from causalgui.front import (build_index, dependency_graph, enumerate_boundaries,
                             expressivity, obligation_summary, read_set, shortlist)
from causalgui.main import (COUNTERS, Candidate, Diagnosis, Model, bind_hypotheses, check_obligations,
                           front_seed, make_plan, normalize_role_spec, rank_hypotheses, scalar_equal)


class PipelineContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="causalgui-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.issue = "Use blue labels. Keep their width unchanged."
        self.context = {"context": [{"file": "main.js", "windows": [{"text": "const actualWidth = 10;"}]}],
                        "scope_limits": {"max_files": 10}}

    def constraint(self, identifier, role="CHANGE_DISTINGUISH", relation="color=blue",
                   entity="label", prop="color"):
        return {"id": identifier, "kind": role, "entity": entity, "property": prop, "context": "",
                "relation": relation,
                "provenance": {"kind": "issue_sentence", "reference": "Use blue labels.", "region": []}}

    def index(self):
        (self.repo / "main.js").write_text(
            "import { helper } from './helper';\n"
            "export function draw(value) {\n"
            "  const width = value - 10;\n"
            "  return width;\n"
            "}\n"
            "function other(state) {\n"
            "  state.cacheIndex = 1;\n"
            "}\n", encoding="utf-8")
        (self.repo / "helper.js").write_text("export const helper = 1;\n", encoding="utf-8")
        (self.repo / "consumer.js").write_text(
            "import { draw as renderDraw } from './main';\nrenderDraw(1);\n", encoding="utf-8")
        return build_index(self.repo, self.repo / "_index")


    def normalize(self, constraints):
        index = {"files": {"main.js": {"masked": "const actualWidth = 10;"}}}
        result = normalize_role_spec({"entities": ["label"], "constraints": deepcopy(constraints)},
                                     self.issue, index, 1)
        return result

    def test_preserve_maps_to_frame_and_invalid_provenance_is_never_must(self):
        change = self.constraint("r")
        preserve = self.constraint("f", "PRESERVE", "unchanged", prop="width")
        preserve["provenance"]["reference"] = "Keep their width unchanged."
        ungrounded = self.constraint("u", prop="height")
        ungrounded["provenance"]["reference"] = "not quoted in the report"
        spec = self.normalize([change, preserve, ungrounded])
        self.assertEqual(spec["constraints"][0]["strength"], "MUST")
        self.assertEqual(spec["constraints"][0]["kind"], "REQUIREMENT")
        self.assertEqual(spec["constraints"][1]["kind"], "FRAME")
        self.assertEqual(spec["constraints"][1]["strength"], "MUST")
        self.assertEqual(spec["constraints"][2]["strength"], "MAY")
        self.assertEqual(spec["provenance_invalid"], 1)

    def test_conflicting_grounded_interpretations_remain_alternatives(self):
        blue = self.constraint("blue", relation="== \"blue\"")
        red = self.constraint("red", role="PRESERVE", relation="== \"red\"")
        spec = self.normalize([blue, red])
        self.assertEqual([item["strength"] for item in spec["constraints"]], ["MAY", "MAY"])
        self.assertEqual([item["ambiguity_reason"] for item in spec["constraints"]],
                         ["conflicting_grounded_interpretations"] * 2)
        self.assertEqual(spec["ambiguity_groups"][0]["constraint_ids"], ["blue", "red"])

    def test_image_region_must_be_normalized_to_unit_square(self):
        pixel = self.constraint("pix")
        pixel["provenance"] = {"kind": "image_region", "reference": "0",
                               "region": [655.0, 615.0, 1875.0, 695.0]}
        unit = self.constraint("unit")
        unit["provenance"] = {"kind": "image_region", "reference": "0", "region": [0.1, 0.2, 0.5, 0.4]}
        outside = self.constraint("outside")
        outside["provenance"] = {"kind": "image_region", "reference": "7", "region": [0.1, 0.2, 0.5, 0.4]}
        spec = self.normalize([pixel, unit, outside])
        strengths = [item["strength"] for item in spec["constraints"]]
        self.assertEqual(strengths, ["MAY", "MUST", "MAY"])
        self.assertEqual(spec["provenance_invalid"], 2)

    def test_context_metadata_cannot_ground_a_source_symbol(self):
        invented = self.constraint("fake")
        invented["provenance"] = {"kind": "base_code_symbol", "reference": "max_files", "region": []}
        genuine = self.constraint("real")
        genuine["provenance"] = {"kind": "base_code_symbol", "reference": "actualWidth", "region": []}
        spec = self.normalize([invented, genuine])
        self.assertFalse(spec["constraints"][0]["provenance_valid"])
        self.assertTrue(spec["constraints"][1]["provenance_valid"])

    def test_boundary_pool_covers_value_decisions_and_ignores_test_dirs(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "hidden.js").write_text("const leak = 1 - 2;\n", encoding="utf-8")
        index = self.index()
        consumers, demanded, _, _ = dependency_graph(index["files"])
        _top, pool = enumerate_boundaries(index["files"], consumers, demanded)
        files = {item["file"] for item in pool}
        self.assertNotIn("tests/hidden.js", files)
        kinds = {item["kind"] for item in pool}
        self.assertEqual(len(pool), len({item["id"] for item in pool}))
        self.assertIn("value_decision", kinds)
        self.assertTrue(any(item["line"] == 3 for item in pool if item["file"] == "main.js"))

    def test_string_match_argument_is_a_matching_boundary(self):
        (self.repo / "main.js").write_text(
            'export function matches(value) { return value.match("label"); }\n', encoding="utf-8")
        index = build_index(self.repo, self.repo / "_string_match_index")
        consumers, demanded, _, _ = dependency_graph(index["files"])
        _top, pool = enumerate_boundaries(index["files"], consumers, demanded)
        matching = [item for item in pool if item["kind"] == "matching"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["expression"], "label")
        self.assertEqual(matching[0]["detail"]["form"], "string_argument")

    def test_front_seed_uses_three_calls_and_passes_owned_interface_downstream(self):
        class FrontModel:
            def __init__(model_self):
                model_self.calls = []
                model_self.limit = 0
                model_self.stage = ""

            def call(model_self, stage, system, user, schema, n=1, temperature=0, history=None):
                model_self.calls.append(stage)
                if stage == "S0_role_spec":
                    value = {"entities": ["label"], "constraints": [{"id": "R1",
                        "kind": "CHANGE_DISTINGUISH", "entity": "label", "property": "color",
                        "context": "", "relation": "== \"blue\"", "provenance": {
                            "kind": "issue_sentence", "reference": "Use blue labels.", "region": []}}]}
                elif stage == "S0_boundaries":
                    candidates = json.loads(user)["boundary_pool"]
                    self.assertLessEqual(len(candidates), 80)
                    boundary = candidates[0]
                    value = {"selections": [{"boundary_id": boundary["id"], "constraint_ids": ["R1"],
                              "readable_symbols": boundary["read_symbols"], "reason": "owned candidate",
                              "selective": True}], "unresolved": []}
                else:
                    selection = json.loads(user)["selections"][0]
                    value = {"ranked_files": ["z.js", selection["file"]], "boundary_plans": [{
                        "id": selection["boundary_id"], "edit_unit": selection["edit_unit"],
                        "edit_reads": selection["read_symbols"], "preserve": []}], "unresolved": []}
                return [json.dumps(value)]

        (self.repo / "main.js").write_text(
            'export function draw(value) {\n  const width = value - 10;\n  return width;\n}\n', encoding="utf-8")
        (self.repo / "a.js").write_text('export const untouchedA = 1;\n', encoding="utf-8")
        (self.repo / "z.js").write_text('export const untouchedZ = 1;\n', encoding="utf-8")
        model = FrontModel()
        request = {"task": {"case": {"repo": "fixture/local", "problem_statement": self.issue,
                   "image_assets": {"problem_statement": []}}},
                   "config": {"worker": {"metadata": {}}}}
        structure, topk, _issue, images, spec, context, facts = front_seed(
            request, self.repo, self.repo / "front_run", model)
        self.assertEqual(model.calls, ["S0_role_spec", "S0_boundaries", "S0_scope"])
        self.assertEqual(model.limit, 11)
        self.assertEqual(images, [])
        self.assertEqual(spec["constraints"][0]["strength"], "MUST")
        selected = context["selected_boundaries"][0]
        self.assertEqual(topk[0], selected["file"])
        self.assertEqual(context["scope_plan"]["ranked_files"][:3], [selected["file"], "a.js", "z.js"])
        self.assertIn(selected["repair_interface"]["G_b"], {
            "matching_expression", "value_expression", "function_body",
            "assignment_expression", "array_expression"})
        self.assertEqual(selected["expressivity"]["verdict"], "UNKNOWN")
        self.assertFalse(selected["expressivity"]["excludes_candidate"])
        self.assertIn("main.js", structure)
        self.assertEqual(facts["files"]["main.js"]["source_sha256"], hashlib.sha256((self.repo / "main.js").read_bytes()).hexdigest())
        self.assertIn(selected["id"], facts["boundary_index"])

    def test_index_falls_back_to_flow_syntax_without_losing_ranges(self):
        (self.repo / "flow.js").write_text(
            "export function typed(value: number): number { return value; }\n", encoding="utf-8")
        index = build_index(self.repo, self.repo / "_flow_index")
        self.assertEqual(index["failed"], [])
        self.assertEqual(index["files"]["flow.js"]["functions"][0]["name"], "typed")

    def test_same_line_functions_keep_distinct_read_candidates(self):
        (self.repo / "main.js").write_text(
            'export const left = (alpha) => alpha ? 1 : 0; export const right = (beta) => beta ? 2 : 0;\n', encoding="utf-8")
        index = build_index(self.repo, self.repo / "_same_line_index")
        consumers, demanded, _, _ = dependency_graph(index["files"])
        _top, pool = enumerate_boundaries(index["files"], consumers, demanded)
        decisions = {item["expression"]: item for item in pool
                     if item["kind"] == "value_decision" and item["expression"] in {"alpha", "beta"}}
        self.assertEqual(set(decisions), {"alpha", "beta"})
        self.assertIn("alpha", decisions["alpha"]["read_symbols"])
        self.assertNotIn("beta", decisions["alpha"]["read_symbols"])
        self.assertIn("beta", decisions["beta"]["read_symbols"])
        self.assertNotIn("alpha", decisions["beta"]["read_symbols"])

    def test_dependency_demand_maps_import_alias_to_target_export(self):
        index = self.index()
        consumers, demanded, unresolved, edges = dependency_graph(index["files"])
        self.assertEqual(demanded["main.js"], {"draw"})
        self.assertIn("consumer.js", consumers["main.js"])
        self.assertEqual(unresolved, [])
        edge = next(item for item in edges if item["from"] == "consumer.js")
        self.assertEqual(edge["demanded_symbols"], ["draw"])
        _top, pool = enumerate_boundaries(index["files"], consumers, demanded)
        self.assertTrue(any(item["kind"] == "output_path" and item["file"] == "main.js"
                            and item["detail"]["demanded_symbol"] == "draw" for item in pool))

    def test_shortlist_uses_role_entities_without_changing_pool(self):
        pool = [
            {"id": "a", "file": "alpha.js", "line": 1, "text": "update alpha", "expression": "alpha",
             "read_symbols": [], "specificity": 50, "detail": {}},
            {"id": "b", "file": "label.js", "line": 2, "text": "paint legendLabel", "expression": "legendLabel",
             "read_symbols": ["legendLabel"], "specificity": 10, "detail": {}},
        ]
        selected = shortlist(pool, "Color the legend.", ["legend label"], limit=1)
        self.assertEqual([item["id"] for item in selected], ["b"])
        self.assertEqual(len(pool), 2)

    def test_expressivity_stays_unknown_without_complete_read_set(self):
        index = self.index()
        consumers, demanded, _, _ = dependency_graph(index["files"])
        _top, pool = enumerate_boundaries(index["files"], consumers, demanded)
        target = next(item for item in pool if item["file"] == "main.js" and item["kind"] == "value_decision")
        conflicting = [self.constraint("a", relation="== 'blue'", entity="value"),
                       self.constraint("b", relation="== 'red'", entity="different")]
        for item in conflicting:
            item["provenance_valid"] = True
        verdict = expressivity(target, conflicting)
        self.assertEqual(verdict["verdict"], "UNKNOWN")
        self.assertIsNone(verdict["certificate"])
        self.assertFalse(verdict["excludes_candidate"])
        self.assertEqual(verdict["reason"], "report_entities_not_bound_to_runtime_objects")
        empty = expressivity(target, [])
        self.assertEqual(empty["verdict"], "UNKNOWN")
        self.assertIsNone(empty["certificate"])

    def test_read_set_reports_parameters_and_locals(self):
        index = self.index()
        entry = index["files"]["main.js"]
        symbols, basis = read_set(entry, 3)
        self.assertIn("value", symbols)
        self.assertIn("width", symbols)
        self.assertIn("helper", symbols)
        self.assertEqual(basis, "function_candidate_upper_bound")

    def test_obligation_summary_lists_the_boundary_line_only_here(self):
        index = self.index()
        target = {"file": "main.js", "line": 5, "detail": {"property": "cacheIndex"}}
        rows = obligation_summary(index["files"], target)
        self.assertEqual([row["line"] for row in rows], [7])
        self.assertTrue(rows[0]["outside_boundary"])
        target.update(line=6, end_line=8)
        rows = obligation_summary(index["files"], target)
        self.assertFalse(rows[0]["outside_boundary"])

    def test_unknown_preserves_topk_and_static_effects_cannot_support(self):
        hypotheses = [{"id": name, "file": name + ".js", "symbol": "draw", "target_property": "width",
                       "question": {"kind": "value", "expression": "this.width", "expected": "20"}}
                      for name in ["extra", "second", "first"]]
        binding = {name: {"symbol_exists": True, "interface_complete": False,
                         "effect_evidence": [{"kind": "property_write", "static_only": True,
                                              "target_binding_verified": False}]}
                   for name in ["extra", "second", "first"]}
        witness = [{"id": "second", "tier": "T1", "execution_ok": True, "reached": True,
                    "values": [10], "scenario_complete": True}]
        root = rank_hypotheses(hypotheses, witness, binding, ["first.js", "second.js"])
        self.assertTrue(root["all_unknown"])
        self.assertEqual(root["ranked_files"], ["first.js", "second.js", "extra.js"])
        self.assertEqual(root["pruned"], [])
        self.assertEqual({item["id"] for item in root["hypotheses"]}, {"extra", "second", "first"})
        witness[0].update(reached=False, values=[])
        self.assertTrue(rank_hypotheses(hypotheses, witness, binding, ["first.js", "second.js"])["all_unknown"])

    def test_request_budget_and_strict_response_schemas(self):
        model = Model("fixture", 42, self.repo / "ledger", 4)
        for stage, count in [("S0", 2), ("S1_S2", 1), ("S5_first", 1), ("S5_samples", 19)]:
            model.reserve(stage, count)
        self.assertEqual((model.calls, model.choices), (4, 23))
        with self.assertRaises(RuntimeError):
            model.reserve("unbudgeted_retry", 1)
        self.assertEqual((model.calls, model.choices), (4, 23))
        for model_type in [Diagnosis, Candidate]:
            schema = model_type.model_json_schema()
            for item in [schema, *schema.get("$defs", {}).values()]:
                if item.get("type") == "object":
                    self.assertFalse(item["additionalProperties"])
                    self.assertEqual(set(item["required"]), set(item["properties"]))

    def test_candidate_schema_excludes_line_windows_and_rejects_legacy_edits(self):
        edit = {"file": "main.js", "search": "const a = 1;", "replace": "const a = 2;", "create": False}
        candidate = {"rationale": "Update the value.", "edits": [edit]}
        self.assertEqual(Candidate.model_validate(candidate).model_dump(), candidate)
        properties = Candidate.model_json_schema()["$defs"]["Edit"]["properties"]
        self.assertEqual(set(properties), {"file", "search", "replace", "create"})
        for field, value in [("start_line", 1), ("end_line", 1), ("start_line", None), ("end_line", None)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                Candidate.model_validate({**candidate, "edits": [{**edit, field: value}]})

    def test_scope_expands_transitive_imports_consumers_and_visual_files(self):
        files = {"a.js": "import './b.js';\nexport function draw() { return 1; }\n",
                 "b.js": "import './c.js';\nexport const b = 1;\n", "c.js": "export const c = 1;\n",
                 "consumer.js": "import './a.js';\n", "style.scss": "$tone: blue;\n",
                 "effect.frag": "uniform float alpha;\n"}
        for name, text in files.items():
            (self.repo / name).write_text(text)
        scope = build_scope(self.repo, {}, ["b.js", "a.js"], [], self.issue)
        self.assertEqual(scope["allowed_files"][:2], ["b.js", "a.js"])
        self.assertEqual(set(scope["allowed_files"]), set(files))
        self.assertTrue(any(edge["from"] == "b.js" and edge["to"] == "c.js" for edge in scope["edges"]))
        limited = build_scope(self.repo, {}, ["a.js"], [], self.issue, max_files=1)
        self.assertTrue(limited["truncated"])
        self.assertTrue(any(item["reason"] == "file_budget" for item in limited["unresolved_dependencies"]))

    def test_long_source_preserves_boundary_and_reports_omissions(self):
        lines = ["// context"] * 650 + ["function draw() {", "  // width = 999;", "  const width = 10;", "  return width;", "}"]
        (self.repo / "main.js").write_text("\n".join(lines) + "\n")
        structure = {"main.js": {"text": lines, "functions": [{"name": "draw", "line": 651, "end_line": 655}], "classes": []}}
        hypothesis = {"id": "draw", "file": "main.js", "symbol": "draw", "line": 653,
                      "target_property": "width", "readable_symbols": ["width"]}
        scope = build_scope(self.repo, structure, ["main.js"], [hypothesis], self.issue)
        context = scope["context"][0]
        self.assertLessEqual(len(context["windows"]), 4)
        self.assertTrue(any(window["start_line"] <= 653 <= window["end_line"] for window in context["windows"]))
        self.assertTrue(context["omitted"])
        self.assertFalse(context["complete"])
        self.assertEqual([effect["line"] for effect in scope["bindings"]["draw"]["effect_evidence"]], [653])
        self.assertFalse(scope["bindings"]["draw"]["interface_complete"])

    def test_unresolved_import_is_not_a_complete_scope(self):
        (self.repo / "main.js").write_text("import unknown from 'unavailable-package';\n")
        scope = build_scope(self.repo, {}, ["main.js"], [], self.issue)
        plan = make_plan(scope, {"constraints": []}, {"new_files": []}, {"ranked_files": ["main.js"]})
        self.assertTrue(scope["unresolved_dependencies"])
        self.assertFalse(plan["scope_complete"])

    def verified_fixture(self, identifier="h", prop="width", value=10):
        hypothesis = {"id": identifier, "file": "main.js", "symbol": "draw", "target_property": prop,
                      "constraint_ids": [identifier],
                      "question": {"kind": "value", "expression": "this." + prop, "expected": "10"}}
        witness = {"id": identifier, "tier": "T1", "execution_ok": True, "reached": True, "values": [value],
                   "evidence": {"kind": "executed_target_write", "verified": True,
                                "target_property": prop, "expression": "this." + prop}}
        return hypothesis, witness

    def test_only_matched_executed_assignment_certificate_supports(self):
        hypothesis, witness = self.verified_fixture()
        bindings = {"h": {"symbol_exists": True}}
        result = rank_hypotheses([hypothesis], [witness], bindings, ["main.js"])
        self.assertEqual(result["hypotheses"][0]["verdict"], "SUPPORTED")
        self.assertFalse(result["hypotheses"][0]["certificate"]["proves_expressibility"])
        for field, value in [("verified", False), ("kind", "property_write"),
                             ("target_property", "height"), ("expression", "other.width")]:
            wrong = deepcopy(witness)
            wrong["evidence"][field] = value
            self.assertTrue(rank_hypotheses([hypothesis], [wrong], bindings, ["main.js"])["all_unknown"])
        witness["execution_ok"] = False
        self.assertTrue(rank_hypotheses([hypothesis], [witness], bindings, ["main.js"])["all_unknown"])

    def test_exact_prediction_refutation_never_excludes_candidate(self):
        hypothesis, witness = self.verified_fixture()
        hypothesis["question"]["expected"] = "20"
        result = rank_hypotheses([hypothesis], [witness], {"h": {"symbol_exists": True}}, ["base.js", "main.js"])
        diagnosed = result["hypotheses"][0]
        self.assertEqual(diagnosed["verdict"], "REFUTED")
        self.assertEqual(diagnosed["certificate"]["refutation_scope"], "hypothesis_prediction_only")
        self.assertFalse(diagnosed["certificate"]["excludes_candidate"])
        self.assertFalse(diagnosed["certificate"]["proves_expressibility"])
        self.assertEqual(result["pruned"], [])
        self.assertEqual(set(result["ranked_files"]), {"base.js", "main.js"})
        for prediction, observed in [("qualitative prediction", [10]), ("20", [10, 20]), ("01", [1])]:
            hypothesis["question"]["expected"], witness["values"] = prediction, observed
            outcome = rank_hypotheses([hypothesis], [witness], {"h": {"symbol_exists": True}}, ["main.js"])
            self.assertEqual(outcome["hypotheses"][0]["verdict"], "UNKNOWN")

    def test_scalar_obligations_require_matching_property_and_expression(self):
        frame, requirement = self.constraint("f", "FRAME", prop="width"), self.constraint("r", relation="== 20", prop="height")
        spec = {"constraints": [dict(frame, strength="MUST"), dict(requirement, strength="MUST")]}
        hf, oldf = self.verified_fixture("f", "width", 10)
        hr, oldr = self.verified_fixture("r", "height", 5)
        newf, newr = deepcopy(oldf), deepcopy(oldr)
        newr["values"] = [20]
        outcomes = check_obligations(spec, [hf, hr], [oldf, oldr], [newf, newr])
        self.assertEqual([item["status"] for item in outcomes], ["PASS", "PASS"])
        self.assertEqual(outcomes[1]["before_status"], "FAIL")
        oldf["values"], newf["values"] = [False], [0]
        self.assertFalse(scalar_equal(False, 0))
        self.assertEqual(check_obligations(spec, [hf], [oldf], [newf])[0]["status"], "FAIL")
        for witness in [oldr, newr]:
            witness["evidence"]["expression"] = "other.height"
        self.assertEqual(check_obligations(spec, [hr], [oldr], [newr])[1]["status"], "UNKNOWN")
        for witness in [oldr, newr]:
            witness["evidence"]["expression"] = "this.height"
        spec["constraints"][1]["property"] = "width"
        self.assertEqual(check_obligations(spec, [hr], [oldr], [newr])[1]["status"], "UNKNOWN")

    def test_uncertified_and_non_scalar_obligations_remain_unknown(self):
        hypothesis, before = self.verified_fixture()
        spec = {"constraints": [dict(self.constraint("h", "FRAME", prop="width"), strength="MUST")]}
        after = deepcopy(before)
        del after["evidence"]
        self.assertEqual(check_obligations(spec, [hypothesis], [before], [after])[0]["status"], "UNKNOWN")
        after = deepcopy(before)
        before["values"] = after["values"] = [{"type": "undefined"}]
        self.assertEqual(check_obligations(spec, [hypothesis], [before], [after])[0]["status"], "UNKNOWN")
        before["values"] = after["values"] = [1]
        spec["constraints"][0].update(kind="REQUIREMENT", relation="== 01")
        self.assertEqual(check_obligations(spec, [hypothesis], [before], [after])[0]["status"], "UNKNOWN")


class S2ContextContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="causalgui-s2-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.issue = "Use blue labels. Keep width unchanged."
        self.prepare_source("import { helper } from './helper.js';\r\n"
                            "function draw(state) {\r\n"
                            "  const width = state.width;\r\n"
                            "  const color = state.active ? 'red' : 'black';\r\n"
                            "  return helper(color, width);\r\n}\r\n", 5)

    def prepare_source(self, text, target):

        (self.repo / "main.js").write_bytes(text.encode("utf-8"))
        (self.repo / "helper.js").write_bytes(b"export function helper(color, width) { return {color, width}; }\n")
        index = build_index(self.repo, self.root / "index")
        _, boundaries = enumerate_boundaries(index["files"], {}, {})
        boundary = next(item for item in boundaries if item["file"] == "main.js" and item["line"] == target
                        and item["kind"] == "value_decision")
        constraints = [{"id": identifier, "kind": role, "entity": "label", "property": prop,
                        "context": context, "relation": relation,
                        "provenance": {"kind": "issue_sentence", "reference": quote, "region": []}}
                       for identifier, role, prop, context, relation, quote in [
                           ("R1", "CHANGE_DISTINGUISH", "color", "active labels", '== "blue"', "Use blue labels."),
                           ("R2", "PRESERVE", "width", "all labels", "unchanged", "Keep width unchanged."),
                           ("R3", "CHANGE_DISTINGUISH", "color", "active labels", '== "green"', "Use blue labels.")]]
        self.spec = normalize_role_spec({"entities": ["label"], "constraints": constraints}, self.issue, index, 0)
        self.identifier = boundary["id"]
        preserve = "Keep all label widths unchanged."
        self.front = {"selected_boundaries": [{"id": boundary["id"],
            **{key: boundary[key] for key in ("file", "line", "end_line", "expression", "mechanism")},
            "candidate_subtype": boundary["kind"],
            "repair_interface": {"G_b": boundary["edit_unit"], "z_b": boundary["read_symbols"], "read_basis": boundary["read_basis"]},
            "K_b": {"constraint_ids": ["R1"], "static_obligations": [], "declared_preserve": [preserve]},
            "expressivity": expressivity(boundary, self.spec["constraints"]), "selection_reason": "Inspect the returned color."}],
            "scope_plan": {"ranked_files": ["main.js", "helper.js"], "unresolved": ["Width has no selected boundary."],
                           "boundary_plans": [{"id": boundary["id"], "edit_reads": ["color"], "preserve": [preserve]}]},
            "candidate_pool": {"total": len(boundaries), "shortlist": len(boundaries)}}
        self.facts = {"files": index["files"], "boundary_index": {item["id"]: item for item in boundaries},
                      "base_commit": "fixture-base", "boundary_unresolved": ["The intended color remains uncertain."],
                      "reference_checks": [{"reason": "fixture_audit_only"}]}

    def build(self, **kwargs):

        return build_hypothesis_payload(self.repo, issue=self.issue, repo_name="fixture/local", base_commit="fixture-base",
                                        spec=self.spec, front_context=self.front, topk=["main.js"], s0_facts=self.facts, **kwargs)

    def test_projection_is_independent_and_preserves_full_state(self):
        original = deepcopy((self.spec, self.front, self.facts))
        payload, audit = self.build()
        self.assertEqual(audit["status"], "ready")
        self.assertEqual((self.spec, self.front, self.facts), original)
        payload["interfaces"][0]["readable_candidates"].append("invented")
        payload["constraints"][0]["provenance"]["reference"] = "changed"
        self.assertEqual((self.spec, self.front, self.facts), original)

    def test_payload_whitelist_and_snapshot(self):
        self.spec["audit_secret"] = "do_not_send"
        self.front["candidate_pool"]["audit_secret"] = "do_not_send"
        self.front["selected_boundaries"][0]["audit_secret"] = "do_not_send"
        payload, audit = self.build()
        self.assertEqual(set(payload), {"protocol", "task", "entities", "constraints", "ambiguity_groups", "interfaces",
                                        "selected_boundaries", "upstream_notes", "source_windows", "gaps", "coverage"})
        self.assertEqual(payload["task"], {"issue": self.issue, "repo": "fixture/local", "base_commit": "fixture-base"})
        self.assertNotIn("do_not_send", json.dumps(payload))
        self.assertNotIn("candidate_pool", json.dumps(payload))
        self.assertNotIn("bindings", payload)
        self.assertEqual(set(payload["constraints"][0]), {"id", "role", "entity", "property", "context", "relation",
                                                          "strength", "ambiguity_reason", "provenance_valid", "provenance"})
        self.assertEqual(set(payload["selected_boundaries"][0]), {"boundary_id", "file", "line", "end_line", "mechanism", "expression",
            "interface_id", "constraint_ids", "note_ids", "source_window_ids", "expressivity", "source_range_complete"})
        self.assertEqual(set(payload["interfaces"][0]), {"id", "edit_unit", "readable_candidates", "read_basis", "focus_symbols"})
        self.assertTrue(all(set(w) == {"id", "file", "source_sha256", "start_line", "end_line", "purposes", "text"}
                            for w in payload["source_windows"]))
        self.assertIsNone(audit["metrics"]["actual_input_tokens"])

    def test_global_constraints_conditions_and_may_survive(self):
        payload, _ = self.build()
        self.assertEqual([c["id"] for c in payload["constraints"]], ["R1", "R2", "R3"])
        self.assertEqual([c["strength"] for c in payload["constraints"]], ["MAY", "MUST", "MAY"])
        self.assertEqual(payload["constraints"][1]["context"], "all labels")
        self.assertEqual(payload["constraints"][1]["role"], "PRESERVE")
        self.assertEqual(payload["ambiguity_groups"][0]["constraint_ids"], ["R1", "R3"])
        self.assertEqual(payload["selected_boundaries"][0]["constraint_ids"], ["R1"])
        self.assertEqual(payload["selected_boundaries"][0]["expressivity"]["verdict"], "UNKNOWN")
        self.assertEqual({g["origin"] for g in payload["gaps"] if g["kind"] == "localization_unresolved"}, {"S0_boundaries", "S0_scope"})

    def test_duplicate_ids_notes_and_overlapping_windows(self):
        self.spec["constraints"].append(deepcopy(self.spec["constraints"][0]))
        self.front["selected_boundaries"].append(deepcopy(self.front["selected_boundaries"][0]))
        payload, audit = self.build()
        self.assertEqual(len(payload["selected_boundaries"]), 1)
        self.assertEqual(len(payload["constraints"]), 3)
        self.assertEqual(sum(n["kind"] == "preserve_suggestion" for n in payload["upstream_notes"]), 1)
        self.assertEqual(audit["deduplicated"]["boundaries"], 1)
        self.assertEqual(audit["deduplicated"]["constraints"], 1)
        self.assertEqual(audit["metrics"]["duplicate_source_lines"], 0)
        self.assertEqual(sum(w["file"] == "main.js" for w in payload["source_windows"]), 1)

    def test_conflicting_id_fails_instead_of_overwriting(self):
        other = deepcopy(self.spec["constraints"][0])
        other["relation"] = "different"
        self.spec["constraints"].append(other)
        payload, audit = self.build()
        self.assertIsNone(payload)
        self.assertEqual(audit["status"], "constraint_id_conflict")

    def test_program_location_overrides_untrusted_fields(self):
        self.front["selected_boundaries"][0].update(file="made-up.js", line=9999, end_line=9999)
        payload, audit = self.build()
        self.assertEqual((payload["selected_boundaries"][0]["file"], payload["selected_boundaries"][0]["line"]), ("main.js", 5))
        self.assertTrue(any(row["reason"] == "program_value_used" for row in audit["reference_checks"]))

    def test_invalid_constraint_reference_is_recorded(self):
        self.front["selected_boundaries"][0]["K_b"]["constraint_ids"].append("missing")
        payload, _ = self.build()
        self.assertEqual(payload["selected_boundaries"][0]["constraint_ids"], ["R1"])
        self.assertTrue(any(g.get("reason") == "unknown_constraint_id" for g in payload["gaps"]))

    def test_invalid_boundary_uses_bounded_fallback(self):
        self.front["selected_boundaries"][0]["id"] = "missing"
        payload, audit = self.build(budget={"fallback_max_files": 1, "fallback_max_windows_per_file": 1, "max_dependency_files": 1})
        self.assertEqual(payload["selected_boundaries"], [])
        self.assertFalse(payload["coverage"]["required_targets_complete"])
        self.assertTrue(any(g.get("reason") == "unknown_boundary_id" for g in payload["gaps"]))
        self.assertLessEqual(len({w["file"] for w in payload["source_windows"]}), 2)
        self.assertEqual(audit["metrics"]["rejected_boundaries"], 1)

    def test_invalid_program_path_and_line_are_not_evidence(self):
        for field, value, reason in [("file", "../outside.js", "invalid_source_path"), ("line", 9999, "line_out_of_range")]:
            with self.subTest(field=field):
                original = self.facts["boundary_index"][self.identifier][field]
                self.facts["boundary_index"][self.identifier][field] = value
                payload, audit = self.build()
                self.assertEqual(payload["selected_boundaries"], [])
                self.assertTrue(any(g.get("reason") == reason for g in audit["gaps"]))
                self.facts["boundary_index"][self.identifier][field] = original

    def test_version_mismatch_is_observable(self):
        (self.repo / "main.js").write_bytes(b"changed\n")
        payload, audit = self.build()
        self.assertIsNone(payload)
        self.assertTrue(any(g["kind"] == "source_version_mismatch" for g in audit["gaps"]))

    def test_unreadable_source_is_observable(self):
        (self.repo / "main.js").unlink()
        payload, audit = self.build()
        self.assertIsNone(payload)
        self.assertTrue(any(g["kind"] == "unreadable_file" for g in audit["gaps"]))

    def test_deep_target_in_large_function_keeps_original_line(self):
        text = "function draw(state) {\n" + "  // unchanged context\n" * 800 + "  return state.color;\n}\n"
        self.prepare_source(text, 802)
        payload, audit = self.build()
        self.assertTrue(any(w["start_line"] <= 802 <= w["end_line"] for w in payload["source_windows"] if w["file"] == "main.js"))
        self.assertTrue(any(w["start_line"] == 1 for w in payload["source_windows"] if w["file"] == "main.js"))
        self.assertTrue(payload["coverage"]["required_targets_complete"])
        self.assertEqual(audit["metrics"]["covered_ranges"], 1)
        self.assertTrue(any(row["file"] == "main.js" and any(r["start_line"] <= 400 <= r["end_line"]
                                                            for r in row["unexpanded_ranges"]) for row in audit["source_coverage"]))

    def test_extremely_long_single_line_cannot_bypass_budget(self):
        raw = ("const message = '" + "x" * 100000 + "';").encode()
        (self.repo / "main.js").write_bytes(raw)
        self.front["selected_boundaries"] = []
        self.facts["files"]["main.js"] = {"functions": [], "source_sha256": hashlib.sha256(raw).hexdigest()}
        payload, audit = self.build()
        self.assertIsNone(payload)
        self.assertEqual(audit["status"], "required_context_exceeds_budget")
        self.assertEqual(audit["failure"]["reason"], "window_byte_budget")

    def test_large_required_declaration_is_split_without_losing_source(self):
        declaration = "const palette = {\r\n" + "".join(f"  shade{i}: 'blue-blue-blue',\r\n" for i in range(700)) + "};\r\n"
        self.prepare_source("function draw(state) {\r\n" + declaration + "  return palette[state.kind];\r\n}\r\n", 704)
        payload, audit = self.build()
        self.assertEqual(audit["status"], "ready")
        windows = [w for w in payload["source_windows"] if w["file"] == "main.js"]
        self.assertGreater(len(windows), 1)
        self.assertTrue(all(len(w["text"].encode()) <= 16384 for w in windows))
        self.assertIn(declaration, "".join(w["text"] for w in windows))
        self.assertTrue(payload["coverage"]["required_targets_complete"])
        self.assertEqual(audit["metrics"]["duplicate_source_lines"], 0)
        self.assertLessEqual(audit["metrics"]["source_utf8_bytes"], 65536)

    def test_embedded_url_is_archived_while_narrative_and_code_remain(self):
        from urllib.parse import quote

        code = "const example = '蓝😀';\r\n" * 1600
        self.issue += "\r\n[Test case](https://example.invalid/demo#text=" + quote(code, safe="") + ")\r\n```js\r\n" + code + "```\r\nKeep the documented behavior.\r\n"
        original = self.issue
        payload, audit = self.build()
        self.assertEqual(audit["status"], "ready")
        projected = payload["task"]["issue"]
        self.assertEqual(projected["protocol"], "issue_blocks_v1")
        self.assertEqual(self.issue, original)
        self.assertEqual(projected["original_sha256"], hashlib.sha256(original.encode()).hexdigest())
        rendered = "".join(block.get("text", "") for block in projected["blocks"])
        self.assertIn("Use blue labels. Keep width unchanged.", rendered)
        self.assertIn("Keep the documented behavior.", rendered)
        self.assertIn(code, rendered)
        omitted = audit["issue_context"]["omissions"]
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]["kind"], "embedded_url")
        self.assertLessEqual(audit["metrics"]["payload"]["utf8_bytes"], 131072)
        self.assertEqual((payload, audit), self.build())

    def test_long_code_issue_uses_traceable_focused_blocks(self):
        code = "".join(f"const value{i} = 'unchanged';\n" for i in range(9000))
        code = code.replace("value5000", "target_marker")
        self.spec["constraints"][0]["context"] = "target_marker"
        self.issue += "\n```js\n" + code + "```\nDo not change the public API.\n"
        payload, audit = self.build()
        self.assertEqual(audit["status"], "ready")
        blocks = payload["task"]["issue"]["blocks"]
        included = "".join(block.get("text", "") for block in blocks)
        self.assertIn("target_marker", included)
        self.assertIn("Do not change the public API.", included)
        self.assertTrue(audit["issue_context"]["omissions"])
        raw = self.issue.encode()
        self.assertEqual(blocks[0]["start_byte"], 0)
        self.assertEqual(blocks[-1]["end_byte"], len(raw))
        for first, second in zip(blocks, blocks[1:]):
            self.assertEqual(first["end_byte"], second["start_byte"])
        for block in blocks:
            original = raw[block["start_byte"]:block["end_byte"]]
            self.assertEqual(hashlib.sha256(original).hexdigest(), block["sha256"])
            if block["included"]:
                self.assertEqual(block["text"].encode(), original)
        self.assertLessEqual(audit["metrics"]["payload"]["utf8_bytes"], 131072)

    def test_mixed_fences_do_not_reclassify_code_as_required_prose(self):
        self.issue += "\n~~~~javascript\n" + ("const code = '```';\n" * 10000) + "~~~~\nKeep width unchanged.\n"
        payload, audit = self.build()
        self.assertEqual(audit["status"], "ready")
        self.assertTrue(audit["issue_context"]["omissions"])
        self.assertIn("Keep width unchanged.", "".join(block.get("text", "") for block in payload["task"]["issue"]["blocks"]))

    def test_required_source_total_budget_has_explicit_failure(self):
        payload, audit = self.build(budget={"max_window_utf8_bytes": 64, "max_source_total_utf8_bytes": 96,
                                          "max_payload_utf8_bytes": 8192})
        self.assertIsNone(payload)
        self.assertEqual(audit["status"], "required_context_exceeds_budget")
        self.assertEqual(audit["failure"]["reason"], "source_total_byte_budget")

    def test_issue_and_constraints_are_never_truncated_to_fit(self):
        self.issue += " source context" * 15000
        payload, audit = self.build()
        self.assertIsNone(payload)
        self.assertEqual(audit["failure"]["reason"], "payload_byte_budget")

    def test_crlf_unicode_and_no_terminal_newline_match_original(self):
        text = (self.repo / "main.js").read_bytes().decode().replace("'red'", "'蓝😀'").rstrip("\r\n")
        self.prepare_source(text, 5)
        payload, audit = self.build()
        for window in payload["source_windows"]:
            raw = (self.repo / window["file"]).read_bytes()
            expected = "".join(raw.decode().splitlines(keepends=True)[window["start_line"] - 1:window["end_line"]])
            self.assertEqual(window["text"], expected)
            self.assertIn(window["text"].encode(), raw)
            self.assertEqual(window["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIn("\r\n", "".join(w["text"] for w in payload["source_windows"]))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(audit["metrics"]["payload"], {"characters": len(serialized), "utf8_bytes": len(serialized.encode())})

    def test_all_references_resolve_to_inline_content(self):
        payload, _ = self.build()
        windows = {w["id"]: w for w in payload["source_windows"]}
        interfaces = {i["id"]: i for i in payload["interfaces"]}
        notes = {n["id"]: n for n in payload["upstream_notes"]}
        for boundary in payload["selected_boundaries"]:
            self.assertIn(boundary["interface_id"], interfaces)
            self.assertTrue(boundary["source_window_ids"])
            self.assertTrue(all(windows[identifier]["text"] for identifier in boundary["source_window_ids"]))
            self.assertTrue(all(identifier in notes for identifier in boundary["note_ids"]))

    def test_same_input_and_config_are_byte_stable(self):
        first, first_audit = self.build()
        second, second_audit = self.build()
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))
        self.assertEqual(first_audit, second_audit)


class AgentContextContracts(unittest.TestCase):
    setUp = S2ContextContracts.setUp
    prepare_source = S2ContextContracts.prepare_source

    def arguments(self, hypotheses=None):

        boundary = self.facts["boundary_index"][self.identifier]
        hypotheses = hypotheses if hypotheses is not None else [{"id": "H1", "boundary_id": self.identifier,
            "boundary_valid": True, "file": "main.js", "symbol": "draw", "line": boundary["line"],
            "mechanism": "output_path", "question": {"kind": "value", "expression": "state.color", "expected": '"red"'},
            "readable_symbols": boundary["read_symbols"], "target_property": "color", "constraint_ids": ["R1"]}]
        structure = {name: entry for name, entry in self.facts["files"].items()}
        return {"task": {"issue": self.issue, "repo": "fixture/local", "base_commit": self.facts["base_commit"]},
                "spec": self.spec, "raw_hypotheses": deepcopy(hypotheses), "hypotheses": hypotheses,
                "front_context": self.front, "s0_facts": self.facts,
                "scope": build_scope(self.repo, structure, ["main.js"], hypotheses, self.issue),
                "source_tools": SourceTools(self.repo, structure), "runtime_capabilities": {}, "images": []}

    def build_agent(self, agent="code", arguments=None, **overrides):

        arguments = self.arguments() if arguments is None else arguments
        builder = build_code_agent_payload if agent == "code" else build_browser_agent_payload
        return builder(self.repo, **{**arguments, **overrides})

    def test_full_state_and_read_permissions_are_not_mutated(self):
        args = self.arguments()
        state = {key: value for key, value in args.items() if key != "source_tools"}
        before, registry = deepcopy(state), deepcopy(args["source_tools"].__dict__)
        for agent in ("code", "browser"):
            payload, _ = self.build_agent(agent, args)
            self.assertIsNotNone(payload)
            payload["hypotheses"][0]["question"]["expression"] = "changed"
            payload["requirements"]["constraints"][0]["provenance"]["reference"] = "changed"
            self.assertEqual(state, before)
            self.assertEqual(args["source_tools"].__dict__, registry)

    def test_distinct_whitelists_and_nested_audit_fields(self):
        args = self.arguments()
        args["spec"]["audit_secret"] = "private_marker"
        args["hypotheses"][0]["audit_secret"] = "private_marker"
        args["hypotheses"][0]["question"]["audit_secret"] = "private_marker"
        args["runtime_capabilities"].update(node_available=True, chromium="private_marker", audit_secret="private_marker")
        common = {"protocol", "task", "requirements", "hypotheses", "location_checks", "source_windows",
                  "source_catalog", "issue_images", "gaps", "coverage"}
        for agent in ("code", "browser"):
            payload, _ = self.build_agent(agent, args)
            extra = {"inspection_context", "tool_environment"} if agent == "code" else {
                "hypothesis_context", "runtime_context", "reproduction_context", "observation_targets"}
            self.assertEqual(set(payload), common | extra)
            self.assertNotIn("private_marker", json.dumps(payload))
            self.assertEqual(set(payload["source_windows"][0]), {"id", "file", "source_id", "source_sha256", "start_line", "end_line", "purposes", "text"})
            self.assertEqual(set(payload["hypotheses"][0]["question"]), {"kind", "expression", "expected"})

    def test_all_global_constraints_and_uncertainty_survive(self):
        for agent in ("code", "browser"):
            payload, _ = self.build_agent(agent)
            constraints = payload["requirements"]["constraints"]
            self.assertEqual([c["id"] for c in constraints], ["R1", "R2", "R3"])
            self.assertEqual([c["strength"] for c in constraints], ["MAY", "MUST", "MAY"])
            self.assertEqual(constraints[1]["role"], "PRESERVE")
            self.assertEqual(constraints[1]["context"], "all labels")
            self.assertTrue(payload["requirements"]["ambiguity_groups"])
            context = payload["inspection_context"] if agent == "code" else payload["hypothesis_context"]
            self.assertEqual(context["boundary_hints"][0]["expressivity"]["verdict"], "UNKNOWN")

    def test_deep_target_stays_visible_across_s2_and_both_agents(self):
        self.prepare_source("function draw(state) {\n" + "  // context\n" * 800 + "  return state.color;\n}\n", 802)
        s2, _ = S2ContextContracts.build(self)
        self.assertTrue(any(w["start_line"] <= 802 <= w["end_line"] for w in s2["source_windows"] if w["file"] == "main.js"))
        for agent in ("code", "browser"):
            payload, audit = self.build_agent(agent)
            self.assertTrue(payload["coverage"]["required_targets_complete"])
            self.assertTrue(any(w["start_line"] <= 802 <= w["end_line"] for w in payload["source_windows"] if w["file"] == "main.js"))
            self.assertTrue(any(w["start_line"] == 1 for w in payload["source_windows"] if w["file"] == "main.js"))
            self.assertEqual(audit["metrics"]["covered_targets"], 1)

    def test_outside_boundary_and_legacy_scope_still_has_source(self):
        (self.repo / "outside.js").write_bytes(b"export function other() {\n  return 7;\n}\n")
        args = self.arguments()
        args["hypotheses"][0].update(file="outside.js", line=2, symbol="other", boundary_id="", boundary_valid=False)
        args["raw_hypotheses"] = deepcopy(args["hypotheses"])
        self.assertNotIn("outside.js", args["scope"]["allowed_files"])
        for agent in ("code", "browser"):
            payload, _ = self.build_agent(agent, args)
            self.assertTrue(any(w["file"] == "outside.js" and w["start_line"] <= 2 <= w["end_line"] for w in payload["source_windows"]))
            self.assertEqual(args["source_tools"].read_files, [])

    def test_original_and_normalized_positions_remain_distinguishable(self):
        args = self.arguments()
        args["raw_hypotheses"][0].update(line=3, symbol="uncertain")
        payload, audit = self.build_agent(arguments=args)
        self.assertEqual([(p["origin"], p["line"]) for p in payload["location_checks"]], [("normalized", 5), ("model_original", 3)])
        self.assertTrue(all(p["source_window_ids"] for p in payload["location_checks"]))
        self.assertIn("line", audit["reference_checks"][0]["normalized_fields"])

    def test_binding_preserves_existing_semantics_and_raw_state(self):
        args = self.arguments()
        original = deepcopy(args["hypotheses"])
        original[0].update(file="invented.js", line=999, symbol="unchanged_symbol")
        raw = deepcopy(original)
        normalized = bind_hypotheses(original, self.front)
        self.assertEqual(original, raw)
        self.assertEqual((normalized[0]["file"], normalized[0]["line"], normalized[0]["symbol"]), ("main.js", 5, "unchanged_symbol"))
        self.assertEqual(normalized[0]["question"], raw[0]["question"])
        self.assertEqual(normalized[0]["constraint_ids"], raw[0]["constraint_ids"])
        normalized[0]["readable_symbols"].append("private")
        self.assertNotIn("private", self.front["selected_boundaries"][0]["repair_interface"]["z_b"])

    def test_static_binding_metadata_survives_without_embedded_source(self):
        args = self.arguments()
        args["scope"]["bindings"]["H1"]["effect_evidence"] = [{"file": "main.js", "line": 4,
            "text": "invented_effect_text_not_source", "kind": "property_write", "static_only": True, "target_binding_verified": False}]
        payload, _ = self.build_agent(arguments=args)
        binding = payload["inspection_context"]["static_bindings"][0]
        self.assertEqual(binding["interface_complete"], args["scope"]["bindings"]["H1"]["interface_complete"])
        self.assertNotIn("invented_effect_text_not_source", json.dumps(payload))
        self.assertTrue(binding["effect_locations"][0]["source_window_ids"])

    def test_long_single_line_is_not_silently_truncated(self):
        raw = ("const color = '" + "蓝" * 20000 + "';\n").encode("utf-8")
        (self.repo / "main.js").write_bytes(raw)
        self.facts["files"]["main.js"] = {"functions": [], "source_sha256": hashlib.sha256(raw).hexdigest()}
        args = self.arguments()
        args["hypotheses"][0].update(line=1, symbol="color", boundary_id="", boundary_valid=False)
        args["raw_hypotheses"] = deepcopy(args["hypotheses"])
        for agent in ("code", "browser"):
            payload, audit = self.build_agent(agent, args)
            self.assertIsNone(payload)
            self.assertEqual(audit["status"], "required_context_exceeds_budget")
            self.assertEqual(audit["failure"]["reason"], "window_byte_budget")

    def test_source_total_and_task_budgets_fail_explicitly(self):
        for budget in ({"max_window_utf8_bytes": 64, "max_source_total_utf8_bytes": 96},
                       {"max_window_utf8_bytes": 128, "max_source_total_utf8_bytes": 512, "max_payload_utf8_bytes": 512}):
            payload, audit = self.build_agent(budget=budget)
            self.assertIsNone(payload)
            self.assertEqual(audit["status"], "required_context_exceeds_budget")

    def test_crlf_unicode_terminal_form_and_overlaps(self):
        text = (self.repo / "main.js").read_bytes().decode().replace("'red'", "'蓝😀'").rstrip("\r\n")
        self.prepare_source(text, 5)
        args = self.arguments()
        args["hypotheses"].append({**deepcopy(args["hypotheses"][0]), "id": "H2", "line": 4})
        args["raw_hypotheses"] = deepcopy(args["hypotheses"])
        for agent in ("code", "browser"):
            payload, audit = self.build_agent(agent, args)
            for w in payload["source_windows"]:
                raw = (self.repo / w["file"]).read_bytes()
                expected = "".join(raw.decode().splitlines(keepends=True)[w["start_line"] - 1:w["end_line"]])
                self.assertEqual(w["text"].encode(), expected.encode())
                self.assertEqual(w["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(audit["metrics"]["duplicate_source_lines"], 0)
            self.assertIn("\r\n", "".join(w["text"] for w in payload["source_windows"]))

    def test_changed_required_source_fails_version_check(self):
        args = self.arguments()
        (self.repo / "main.js").write_bytes(b"changed\n")
        payload, audit = self.build_agent(arguments=args)
        self.assertIsNone(payload)
        self.assertEqual(audit["status"], "required_source_unavailable")
        self.assertTrue(any(g["kind"] == "source_version_mismatch" for g in audit["gaps"]))

    def test_invalid_location_uses_bounded_observable_fallback(self):
        args = self.arguments()
        args["hypotheses"][0]["line"] = 99999
        args["raw_hypotheses"] = deepcopy(args["hypotheses"])
        payload, audit = self.build_agent(arguments=args, budget={"fallback_max_files": 1, "fallback_max_windows_per_file": 1})
        self.assertTrue(payload["coverage"]["fallback"])
        self.assertFalse(payload["coverage"]["required_targets_complete"])
        self.assertTrue(any(g["kind"] == "invalid_reference" for g in payload["gaps"]))
        self.assertEqual(audit["metrics"]["valid_targets"], 0)

    def test_directory_location_does_not_hide_other_valid_targets(self):
        (self.repo / "nested").mkdir()
        (self.repo / "nested/index.js").write_bytes(b"export const value = 1;\n")
        args = self.arguments()
        args["hypotheses"].append({**deepcopy(args["hypotheses"][0]), "id": "H2", "file": "nested", "line": 1, "boundary_id": "", "boundary_valid": False})
        args["raw_hypotheses"] = deepcopy(args["hypotheses"])
        payload, audit = self.build_agent(arguments=args)
        self.assertIsNotNone(payload)
        self.assertEqual(audit["required_missing"], [])
        self.assertEqual(payload["location_checks"][1]["status"], "invalid")
        self.assertIsNone(payload["location_checks"][1]["source_id"])
        self.assertEqual(audit["metrics"]["covered_targets"], 1)

    def test_visible_ids_and_complete_legacy_discovery_match_executor(self):
        args = self.arguments()
        payload, _ = self.build_agent(arguments=args)
        tools = args["source_tools"]
        self.assertEqual(payload["source_catalog"]["directories"], tools.catalog()["directories"])
        for window in payload["source_windows"]:
            self.assertEqual(tools.paths[window["source_id"]], window["file"])
        self.assertEqual(tools.read_files, [])

    def test_evidence_projection_keeps_large_ids_and_root_pagination(self):
        for number in range(205):
            (self.repo / f"a{number:03}.js").write_bytes(b"export const n = 1;\n")
        for command in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Base"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, capture_output=True)
        base = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.facts["base_commit"] = base
        args = self.arguments()
        tools = args["source_tools"] = SourceTools(self.repo, {}, evidence=True)
        payload, _ = self.build_agent(arguments=args)
        self.assertGreater(tools.ids["main.js"], 200)
        self.assertEqual(next(w["source_id"] for w in payload["source_windows"] if w["file"] == "main.js"), tools.ids["main.js"])
        self.assertTrue(payload["source_catalog"]["file_listing_truncated"])
        found = tools.execute({"name": "list_sources", "source_id": 0, "query": "a204", "start_line": 0, "end_line": 0}, self.root / "listing")
        self.assertEqual(found["files"][0]["source_id"], tools.ids["a204.js"])
        self.assertEqual(tools.read_files, [])

    def test_images_keep_zero_based_indices_and_transport_budget(self):
        args = self.arguments()
        args["images"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 12000}}]
        payload, audit = self.build_agent("browser", args)
        self.assertEqual(payload["issue_images"], {"index_base": 0, "count": 1, "indices": [0]})
        initial = {"role": "user", "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}, *args["images"]]}
        self.assertEqual(audit["metrics"]["initial_message"]["utf8_bytes"], len(json.dumps(initial, ensure_ascii=False).encode()))
        payload, audit = self.build_agent("browser", args, budget={"max_window_utf8_bytes": 1024,
            "max_source_total_utf8_bytes": 2048, "max_payload_utf8_bytes": 10000, "max_initial_message_utf8_bytes": 10000})
        self.assertIsNone(payload)
        self.assertEqual(audit["status"], "required_context_exceeds_budget")

    def test_same_frozen_input_has_identical_payload_and_audit(self):
        args = self.arguments()
        for agent in ("code", "browser"):
            first = self.build_agent(agent, args)
            second = self.build_agent(agent, args)
            self.assertEqual(first, second)
            self.assertIsNone(first[1]["metrics"]["actual_input_tokens"])

    def test_config_rejects_unknown_or_nonpositive_limits(self):
        for config in ({"max_files": 3}, {"max_window_utf8_bytes": 0}, {"context_radius": True}):
            with self.subTest(config=config), self.assertRaises(AssertionError):
                s2_context_config(config)


if __name__ == "__main__":
    unittest.main()
