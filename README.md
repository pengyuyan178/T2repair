<p align="center">
  <img src="assets/t2repair-banner.png" alt="Centered T2Repair nameplate held by two small mascots, surrounded by illustrated software inspection, repair and validation icons" width="960">
</p>

# T2Repair

**T2Repair: Thinking It Through and Trying It Out for Visual Bug Repair**

[Run T2Repair](docs/RUNNING.md) · [Paper figures (PDF)](figures/README.md) · [Figure 8 trajectory](trajectories/fig8-prism-1853/README.md) · [Release contents](docs/RELEASE.md)

T2Repair repairs visual bugs in JavaScript projects by combining source-code investigation (*Thinking It Through*) with runtime reproduction (*Trying It Out*). A screenshot reveals what went wrong; the method investigates how the program produced it and what a patch must preserve.

This repository contains the current T2Repair implementation, a standalone container launcher, all eight paper figures as PDFs, and the recorded DeepSeek trajectory behind Figure 8. Baseline implementations are outside this release.

## How it works

1. **Ground the issue.** Read the issue and its images, derive requirements, index the base source, and form shared hypotheses about relevant program behavior.
2. **Investigate in two roles.** The Code Agent inspects source paths and dependencies. The Browser Agent executes the real base program, refines its reproduction, and records observations. They run Code → Browser with separate contexts and do not revise each other's reports.
3. **Generate patches from evidence.** A separate patch generator receives both reports and tool observations, then produces five candidate patches. The investigation agents do not edit the target source.
4. **Validate and replay.** Check candidates and replay the Browser Agent's frozen scene and completed actions without further model calls. Select a patch using the available evidence, preserving `UNKNOWN` when a check cannot establish a result.

The default budgets are **2 Code Agent calls**, **3 Browser Agent calls**, and **5 candidates**, with seed **42**. Each agent's final call is reserved for its report. Screenshots are retained as evidence; replay does not send them to a model for a new visual judgment.

See the [framework](figures/fig3-framework.pdf) and [runtime/configuration guide](docs/RUNNING.md).

## Thinking It Through

The Code Agent inspects definitions, callers and dependencies to trace state changes and decision logic. It supplies evidence-linked findings and repair suggestions to the patch generator.

<p align="center">
  <img src="assets/deepseek-code-agent.png" alt="A proud DeepSeek whale girl directing the Code Agent: Trace the state to the decision, and bring me the evidence." width="960">
</p>

## Trying It Out

The Browser Agent tests the hypotheses against the running base program, collecting runtime events and available browser observations. Its final reproduction script and completed actions are frozen for replay on candidate patches.

<p align="center">
  <img src="assets/glm-browser-agent.png" alt="A composed GLM directing the Browser Agent: Recreate the scene, probe the state, and keep it replayable." width="960">
</p>

*DeepSeek and GLM personify the two roles in these illustrations; the implementation uses a shared, configurable model backend for both agents.*

## Figure 8: evidence that changes the diagnosis

On `PrismJS__prism-1853`, source inspection suggests that a comment rule interferes with strings containing `/*`. Browser execution reveals a more precise failure: the final `"C":"C"` member splits into **text → string → text**, with no comment token in the issue output. It also establishes that real comments must still work.

The selected patch restores **property → operator → string**, and frozen replay preserves the line/block comment controls. The archived official evaluation records a pass. The [walkthrough](trajectories/fig8-prism-1853/README.md) connects each step to the original requests, observations, patch, and replay, including the checks that remain unknown.

Verify the 451 archived files and the figure's key observations offline:

```bash
python scripts/verify_fig8.py
```

Python 3.11+ is sufficient for this archive check; it needs no API key or extra packages.

## Run the method

Build the Linux runtime:

```bash
docker build -t t2repair:local .
```

Then follow [Run T2Repair](docs/RUNNING.md) to prepare a base checkout and issue images, install the target project's dependencies, configure an OpenAI-compatible model endpoint, and run `prepare → check → run → export`. The same configured backend serves both investigation roles and patch generation.

The launcher runs one case at a time. Official benchmark evaluation is a separate step; the recorded Figure 8 outcome was not regenerated for this release.

## Paper figures

| Figure | PDF |
| --- | --- |
| 1 | [Visual bug scenarios](figures/fig1-visual-scenarios.pdf) |
| 2 | [Motivating example](figures/fig2-motivating-example.pdf) |
| 3 | [T2Repair framework](figures/fig3-framework.pdf) |
| 4 | [Code Agent investigation](figures/fig4-code-investigation.pdf) |
| 5 | [Browser Agent investigation](figures/fig5-browser-investigation.pdf) |
| 6 | [Repair overlap](figures/fig6-repair-overlap.pdf) |
| 7 | [Agent ablation overlap](figures/fig7-ablation-overlap.pdf) |
| 8 | [Repair trajectory on Prism #1853](figures/fig8-repair-trajectory.pdf) |

## Code map

| Location | Purpose |
| --- | --- |
| [code/causalgui/front.py](code/causalgui/front.py) | Base-source indexing, candidate boundaries, and grounded input construction |
| [code/causalgui/code_agent.py](code/causalgui/code_agent.py) | Source tools, inspection contexts, and repair scope |
| [code/causalgui/browser_agent.py](code/causalgui/browser_agent.py) | Runtime scenes, browser actions, observations, replay, and candidate checks |
| [code/causalgui/feedback.py](code/causalgui/feedback.py) | Evidence projection into bounded tool feedback |
| [code/causalgui/s5_context.py](code/causalgui/s5_context.py) | Evidence-aware patch-generator context |
| [code/causalgui/synthesis.py](code/causalgui/synthesis.py) | Atomic candidate edits and patch construction |
| [code/causalgui/main.py](code/causalgui/main.py) | Schemas, model calls, investigation, candidate generation, and selection |
| [code/causalgui/cli.py](code/causalgui/cli.py) | Standalone release commands |
| [tests/](tests/) | Contract, browser, synthesis, feedback, and launcher checks |
| [trajectories/fig8-prism-1853/](trajectories/fig8-prism-1853/) | Recorded Figure 8 evidence |

`causalgui` is the implementation's internal Python package name. The released method is T2Repair. Ordinary code comments and docstrings were removed with executable-AST equivalence checks; model prompts and archived evidence were preserved. See [release provenance](docs/RELEASE.md).
