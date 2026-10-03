# Run T2Repair

The release CLI runs the current T2Repair method on one prepared case. Generation uses a Linux container, a checkout of the task's base commit, locally installed project dependencies, issue images, and a configurable OpenAI-compatible model endpoint. `prepare` and `export` also work on Windows.

The algorithm package is `causalgui`; the installed command is `t2repair`. Examples below use Bash from the repository root. For Windows, use WSL for the container commands or translate the bind-mount paths for Docker Desktop.

## 1. Build the runtime and install the preparation command

```bash
docker build -t t2repair:local .
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
t2repair --help
```

Python 3.11+ is required. The container includes Python dependencies, Git, Node/npm, Babel's parser, and Debian's Chromium package. The Browser Agent launches `/usr/bin/chromium` and connects through Playwright's CDP interface. The target project's own dependencies are installed separately in step 3.

When package downloads are slow, the build accepts `--build-arg PIP_INDEX_URL=YOUR_PYPI_MIRROR` and `--build-arg DEBIAN_MIRROR=YOUR_DEBIAN_MIRROR` for Python and system packages. Defaults use the upstream package servers.

## 2. Prepare the task and its base checkout

Provide a local Git repository containing the task's base commit and a UTF-8 JSON task file with these fields:

```json
{
  "instance_id": "owner__project-123",
  "repo": "owner/project",
  "base_commit": "FULL_BASE_COMMIT_HASH",
  "problem_statement": "The original issue text and reproduction steps."
}
```

Download the issue's input images locally and pass them explicitly with repeated `--image` arguments. They are attached to the model in that order. The launcher does not fetch remote image URLs or read image locations from the input JSON automatically.

```bash
t2repair prepare \
  --repo /absolute/path/to/project \
  --task /absolute/path/to/task.json \
  --image /absolute/path/to/issue.png \
  --config configs/default.json \
  --model YOUR_ENDPOINT_MODEL_ID \
  --output runs/example
```

This creates a fresh, shallow checkout at the base commit, copies the images, and writes `request.json`. Only the instance ID, repository name, base commit, problem statement, and explicitly supplied issue images enter the generated task. Gold patches, test patches, and evaluator answers are not copied from a benchmark row. The output directory must be new.

For the archived Figure 8 case, the input task is [input_context/task.json](../trajectories/fig8-prism-1853/input_context/task.json), the image is [assets/issue_image_01.png](../trajectories/fig8-prism-1853/assets/issue_image_01.png), and the base commit is `2f9c9261bc1454899266929711d5842a3675e467`. A new run requires a separate Prism checkout and output directory; it does not overwrite the archive.

## 3. Install the target project's dependencies

Install dependencies inside the prepared `runs/example/repo`, using the package manager and versions expected by that base revision. For a compatible npm project with a lockfile, the command is:

```bash
docker run --rm --init \
  --mount "type=bind,source=$PWD/runs/example,target=/task" \
  --workdir /task/repo --entrypoint npm \
  t2repair:local ci
```

Other projects may require Yarn, a different Node version, or additional build/system packages. Extend the runtime for those projects and follow their base-version setup instructions. Keep tracked source and lockfiles at the base commit. T2Repair snapshots tracked base files and links installed dependency directories into disposable execution copies.

Root `node_modules` is detected automatically. For nested packages, pass each repository-relative location during `prepare`, for example `--dependency-root packages/app/node_modules`. This release does not automatically reconstruct every benchmark project's historical dependency environment.

## 4. Check the runtime

```bash
docker run --rm --init --ipc=host \
  --mount "type=bind,source=$PWD/runs/example,target=/task" \
  t2repair:local check
```

Inspect `runs/example/preflight/result.json` for parser, Chromium, entry-loading, and test-runner availability before generation. The command records capabilities; an unavailable renderer or project test is reported and can limit later evidence. It is not an official repair verdict. `check` makes no model calls.

## 5. Configure the backend and generate a patch

Copy [.env.example](../.env.example) to a local `.env` and set `OPENAI_API_KEY` and `OPENAI_BASE_URL` for your endpoint. The same model serves grounding, both agents, and patch generation. Use a model and endpoint that accept the issue's image inputs and the selected structured-output format.

```bash
docker run --rm --init --ipc=host \
  --env-file .env \
  --mount "type=bind,source=$PWD/runs/example,target=/task" \
  t2repair:local run
```

`run` calls the configured model API. It requires preflight output and refuses to overwrite an existing model ledger. Prepare another directory for another run. Results include `patch.diff`, `case_result.json`, `predictions.json`, model requests/responses, agent evidence, candidate checks, and replay artifacts. The target source is kept at the base commit; candidate edits are evaluated in disposable copies.

## Configuration

[configs/default.json](../configs/default.json) sets the released method defaults:

| Setting | Default / behavior |
| --- | --- |
| Seed | 42; the container also sets `PYTHONHASHSEED=42` |
| Investigation budgets | Code: 2 calls; Browser: 3 calls, including each final report |
| Candidate count | 5; one initial candidate and four later samples |
| Agent contexts | Independent; fixed Code → Browser execution order |
| Replay | Final scene and completed actions frozen; no replay model calls |
| Structured output | `json_schema`; override at preparation with `--response-mode json_object` or `prompt_only` if required by the endpoint |
| Format recovery | At most one format correction per invalid sample |
| Output limit | 131072 tokens per choice; adjust to the endpoint's supported limit in a copied config |
| Sample timeout | 1800 seconds |
| Case model budget | 7200 seconds with 600 seconds reserved for finalization; this is a model deadline, not a container-wide hard kill |
| Reasoning effort | Optional `--reasoning-effort low`, `medium`, or `high`; omit when unsupported |

To run an agent ablation, add `--agent-ablation without-code` or `--agent-ablation without-browser` to `prepare`. The removed role's calls are not reassigned. Shared hypotheses and the five-candidate budget remain. These are T2Repair variants; no baseline implementations are bundled.

## Export and official evaluation

The runner writes `predictions.json` automatically. Export it again, or export a recorded result, with:

```bash
t2repair export --run runs/example --output runs/example/predictions.json
```

The output is a JSON array containing `instance_id`, `model_name_or_path`, and `model_patch`. Patch newlines are preserved. Internal syntax checks, regression checks, and replay scores are distinct from the official benchmark result.

In a separate environment with the official SWE-bench harness installed, evaluate the predictions following the [Multimodal dataset's evaluation instructions](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multimodal/blob/main/README.md):

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Multimodal \
  --split test \
  --predictions_path /absolute/path/to/predictions.json \
  --instance_ids YOUR_INSTANCE_ID \
  --max_workers 1 \
  --run_id t2repair-example
```

Use the benchmark revision and project environments corresponding to the experiment you are comparing. This repository ships the Figure 8 trace and its saved result row, not the full 480-case batch controller or dataset. No new model generation or official benchmark evaluation was performed when assembling the release.

## Checks without model calls

Inspect Figure 8 using only Python's standard library:

```bash
python scripts/verify_fig8.py
```

Run the implementation tests inside the built image:

```bash
docker run --rm --init --ipc=host \
  --mount "type=bind,source=$PWD/tests,target=/tests,readonly" \
  --entrypoint python t2repair:local /tests/run_suite.py
```

The tests cover source grounding, atomic patch application, bounded feedback, browser interactions and replay, format recovery, role ablations, interrupted generation, stage budgets, and release commands. Model responses are scripted or mocked; browser and Git operations are real. The full runtime suite targets Linux because it uses POSIX commands and symbolic links.
