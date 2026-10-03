import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from causalgui.cli import configure, execute, export_prediction, prepare


class ReleaseCommands(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="t2repair-release-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "--quiet")
        (self.source / "main.js").write_text("const base = 1;\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.source / "main.js").write_text("const future = 2;\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "future")
        self.future = self.git("rev-parse", "HEAD")
        self.task = self.root / "task.json"
        self.task.write_text(json.dumps({
            "instance_id": "fixture-1", "repo": "fixture/local", "base_commit": self.base,
            "problem_statement": "Preserve the base value.", "patch": "GOLD_PATCH_MUST_NOT_BE_VISIBLE",
            "test_patch": "HIDDEN_TEST_MUST_NOT_BE_VISIBLE", "image_assets": {"problem_statement": ["file:///private/image.png"]}
        }), encoding="utf-8")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"model": "", "harness_policy": {"response_mode": "json_schema"}}), encoding="utf-8")
        self.output = self.root / "case"
        self.args = argparse.Namespace(repo=self.source, task=self.task, config=self.config, output=self.output,
            model="fixture-model", image=[], dependency_root=[], agent_ablation=None,
            response_mode=None, reasoning_effort=None)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], text=True).strip()

    def prepared(self):
        with redirect_stdout(io.StringIO()):
            prepare(self.args)
        return json.loads((self.output / "request.json").read_text(encoding="utf-8"))

    def test_preparation_publishes_only_the_base_and_allowed_task_fields(self):
        request = self.prepared()
        repo = self.output / "repo"
        commits = subprocess.check_output(["git", "-C", str(repo), "rev-list", "--all"], text=True).splitlines()
        self.assertEqual(commits, [self.base])
        self.assertNotEqual(commits, [self.future])
        self.assertEqual((repo / "main.js").read_text(), "const base = 1;\n")
        self.assertEqual(subprocess.check_output(["git", "-C", str(repo), "remote"], text=True), "")
        visible = request["task"]["case"]
        self.assertEqual(set(visible), {"instance_id", "repo", "base_commit", "problem_statement", "image_assets"})
        self.assertEqual(visible["image_assets"]["problem_statement"], [])
        self.assertNotIn("GOLD_PATCH", json.dumps(request))
        self.assertNotIn("HIDDEN_TEST", json.dumps(request))
        self.assertEqual(request["repo_path"], "/task/repo")

    def test_image_bytes_are_preserved_and_paths_are_relocated(self):
        picture = self.root / "input.png"
        picture.write_bytes(b"fixture-image-bytes")
        self.args.image = [picture]
        request = self.prepared()
        self.assertEqual((self.output / "issue_images/issue_image_01.png").read_bytes(), picture.read_bytes())
        self.assertEqual(request["task"]["case"]["image_assets"]["problem_statement"], ["file:///task/issue_images/issue_image_01.png"])

    def test_ablation_keeps_the_other_budgets(self):
        self.args.agent_ablation = "without-browser"
        self.args.response_mode = "json_object"
        self.args.reasoning_effort = "high"
        metadata = self.prepared()["config"]["worker"]["metadata"]
        self.assertEqual(metadata["agent_limits"], {"code": 2, "browser": 0, "candidates": 5})
        self.assertEqual(metadata["harness_policy"]["response_mode"], "json_object")
        self.assertEqual(metadata["model_transport"], {"reasoning_effort": "high"})

    def test_existing_run_cannot_be_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep")
        with self.assertRaisesRegex(ValueError, "new output"):
            prepare(self.args)
        self.assertEqual(marker.read_text(), "keep")

    def test_missing_model_leaves_no_partial_checkout(self):
        self.args.model = None
        with self.assertRaisesRegex(ValueError, "--model"):
            prepare(self.args)
        self.assertFalse(self.output.exists())

    def test_prediction_export_preserves_patch_newlines(self):
        self.output.mkdir()
        (self.output / "case_result.json").write_text(json.dumps({"instance_id": "fixture-1", "model_name_or_path": "fixture-model"}))
        contents = b"diff --git a/main.js b/main.js\n@@ -1 +1 @@\n-old\r\n+new\r\n"
        (self.output / "patch.diff").write_bytes(contents)
        destination = self.output / "predictions.json"
        export_prediction(self.output, destination)
        records = json.loads(destination.read_text())
        self.assertEqual(records, [{"instance_id": "fixture-1", "model_name_or_path": "fixture-model", "model_patch": contents.decode()}])

    def test_execution_rejects_an_unmounted_output(self):
        with self.assertRaisesRegex(ValueError, "Linux container"):
            configure({"output_dir": str(self.output)})

    def test_model_is_never_called_before_preflight_or_on_an_existing_run(self):
        self.output.mkdir()
        request = self.output / "request.json"
        request.write_text("{}")
        args = argparse.Namespace(command="run", request=request)
        with patch("causalgui.cli.configure", return_value=self.output), patch("causalgui.cli.run") as model_run:
            with self.assertRaisesRegex(ValueError, "Run check"):
                execute(args)
            (self.output / "trajectory").mkdir()
            (self.output / "trajectory/model_ledger.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "already contains"):
                execute(args)
            model_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
