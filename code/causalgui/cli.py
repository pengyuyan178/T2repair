import argparse
from functools import partial
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .main import agent_budget, preflight, run, write_json


def git(repo, *arguments):
    return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True).strip()


def prepare(args):
    source, output = args.repo.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError("Choose a new output directory for each run.")
    raw = json.loads(args.task.read_text(encoding="utf-8-sig"))
    case = {key: raw[key] for key in ("instance_id", "repo", "base_commit", "problem_statement")}
    base = git(source, "rev-parse", case["base_commit"] + "^{commit}")
    case["base_commit"] = base
    metadata = json.loads(args.config.read_text(encoding="utf-8-sig"))
    metadata["model"] = args.model or metadata.get("model")
    if not metadata["model"]:
        raise ValueError("Specify --model or set model in the configuration.")
    metadata["agent_ablation"] = args.agent_ablation or metadata.get("agent_ablation", "full")
    metadata["agent_limits"] = agent_budget(metadata)
    if args.response_mode:
        metadata["harness_policy"]["response_mode"] = args.response_mode
    if args.reasoning_effort:
        metadata["model_transport"] = {"reasoning_effort": args.reasoning_effort}
    image_paths = [path.resolve(strict=True) for path in args.image]
    output.mkdir(parents=True)
    repository = output / "repo"
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    git(repository, "config", "core.autocrlf", "false")
    subprocess.run(["git", "-C", str(repository), "-c", "protocol.file.allow=always", "fetch", "--quiet", "--depth=1", source.as_uri(), base], check=True)
    subprocess.run(["git", "-C", str(repository), "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
    assert git(repository, "rev-list", "--all").splitlines() == [base]
    images = []
    for index, source_image in enumerate(image_paths, 1):
        name = "issue_image_%02d%s" % (index, source_image.suffix.lower())
        target = output / "issue_images" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source_image, target)
        images.append("file:///task/issue_images/" + name)
    case["image_assets"] = {"problem_statement": images}
    request = {"task": {"case": case}, "config": {"seed": 42, "worker": {"metadata": metadata}},
               "repo_path": "/task/repo", "output_dir": "/task", "case_dir": "/task",
               "runtime": {"dependency_paths": args.dependency_root}}
    write_json(output / "request.json", request)
    write_json(output / "input_context/task.json", case)
    print(json.dumps({"prepared": str(output), "base_commit": base, "images": len(images), "agent_limits": metadata["agent_limits"]}))


def configure(request):
    root = Path(request["output_dir"]).resolve()
    if sys.platform != "linux" or root != Path("/task"):
        raise ValueError("Run check/run in the Linux container with the prepared directory mounted at /task.")
    if os.environ.get("PYTHONHASHSEED") != "42":
        raise ValueError("Start Python with PYTHONHASHSEED=42.")
    repository = Path(request["repo_path"]).resolve()
    if repository != root / "repo":
        raise ValueError("The prepared base repository must be /task/repo.")
    case = request["task"]["case"]
    if git(repository, "rev-parse", "HEAD") != case["base_commit"] or git(repository, "diff", "HEAD", "--"):
        raise ValueError("The prepared repository must match the unchanged task base commit.")
    paths = request.get("runtime", {}).get("dependency_paths", [])
    if not paths and (repository / "node_modules").is_dir():
        paths = ["node_modules"]
    for name in paths:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts or relative.name != "node_modules":
            raise ValueError("Dependency roots must be repository-relative node_modules paths.")
        if not (repository / relative).is_dir():
            raise ValueError("Install the target project's dependencies before running: " + name)
    write_json(repository / ".git/causalgui_dependencies.json", {"paths": paths, "repo_links": {}})
    os.environ["CAUSALGUI_EVIDENCE_POLICY"] = "base"
    os.environ["CAUSALGUI_TOOL_PYTHON"] = sys.executable
    os.environ.setdefault("CAUSALGUI_SCRATCH", str(root / "scratch"))
    if not os.environ.get("CAUSALGUI_CHROMIUM"):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as browser:
            os.environ["CAUSALGUI_CHROMIUM"] = browser.chromium.executable_path
    return root


def execute(args):
    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    root = configure(request)
    if args.command == "check":
        report = preflight(request)
        write_json(root / "preflight/result.json", report)
        print(json.dumps(report))
        return
    if (root / "trajectory/model_ledger.json").exists():
        raise ValueError("This directory already contains a model run. Prepare a new directory.")
    if not (root / "preflight/result.json").is_file():
        raise ValueError("Run check before run.")
    settings = request["config"]["worker"]["metadata"]
    policy = settings["harness_policy"]
    request.setdefault("runtime", {})["model_deadline_unix"] = time.time() + policy["case_timeout_seconds"] - policy["finalize_reserve_seconds"]
    effort = settings.get("model_transport", {}).get("reasoning_effort")
    if effort:
        import openai
        original = openai.OpenAI
        def client(**kwargs):
            instance = original(**kwargs)
            instance.chat.completions.create = partial(instance.chat.completions.create, reasoning_effort=effort)
            return instance
        openai.OpenAI = client
    try:
        result = run(request)
    finally:
        if effort:
            openai.OpenAI = original
    export_prediction(root, root / "predictions.json")
    print(json.dumps(result))


def export_prediction(directory, destination):
    result = json.loads((directory / "case_result.json").read_text(encoding="utf-8"))
    record = {"instance_id": result["instance_id"], "model_name_or_path": result["model_name_or_path"],
              "model_patch": (directory / "patch.diff").read_bytes().decode("utf-8")}
    write_json(destination, [record])


def main():
    parser = argparse.ArgumentParser(description="Prepare a base checkout and run the current T2Repair method.")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--task", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--model")
    p.add_argument("--image", type=Path, action="append", default=[])
    p.add_argument("--dependency-root", action="append", default=[])
    p.add_argument("--agent-ablation", choices=("full", "without-code", "without-browser"))
    p.add_argument("--response-mode", choices=("json_schema", "json_object", "prompt_only"))
    p.add_argument("--reasoning-effort", choices=("low", "medium", "high"))
    for name in ("check", "run"):
        commands.add_parser(name).add_argument("--request", type=Path, default=Path("/task/request.json"))
    p = commands.add_parser("export")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "export":
        export_prediction(args.run, args.output)
    else:
        execute(args)
