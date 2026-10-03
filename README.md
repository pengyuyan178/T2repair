<p align="center">
  <img src="assets/t2repair-banner.png" alt="Centered T2Repair nameplate held by two small mascots, surrounded by illustrated software inspection, repair and validation icons" width="960">
</p>

# T2Repair

**Seeing Is Just the Start: Thinking It Through and Trying It Out for Visual Bug Repair**

T2Repair is a research project for visual bug repair on SWE-bench Multimodal. It combines source-code investigation (*Thinking It Through*) with runtime reproduction and observation (*Trying It Out*).

The agents investigate shared debugging hypotheses with separate contexts. Their reports and tool observations then inform patch generation.

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

## Repository status

This repository currently contains the project overview and visual assets. Code and supporting research materials will be added later.
