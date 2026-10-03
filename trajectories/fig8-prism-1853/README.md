# Figure 8: why source inspection and execution need each other

[Paper figure (PDF)](../../figures/fig8-repair-trajectory.pdf) · [Final patch](patch.diff) · [Figure evidence](figure-evidence.json) · [File hashes](manifest.json)

This is the recorded **DeepSeek** run for `PrismJS__prism-1853`, titled **`/* breaks JSON tokenization`**, at base commit `2f9c9261bc1454899266929711d5842a3675e467`. The archive includes model requests and responses, both agents' tool observations, five candidate patches, validation, and frozen replay.

The figure labels the original report as `Issue #1852`; `1853` in the benchmark instance ID is the repair PR number.

## The reported failure

The [task](input_context/task.json) and [issue screenshot](assets/issue_image_01.png) describe incorrect highlighting after a string containing `/*`:

```json
{"A":"/*","B":"B","C":"C"}
```

The last member loses its expected property/value highlighting. The important question is how the grammar produces that visible corruption while still supporting real comments.

## Follow the investigation

| Stage | What happened | Recorded evidence |
| --- | --- | --- |
| Shared hypotheses | Eight hypotheses cover the comment regex, grammar order, string matching, and the resulting tokens. These are predictions about the base program. | [Hypotheses](trajectory/S2_hypotheses.json) |
| Code Agent, 2 calls | Reads the 19-line JSON grammar and searches greedy matching in the Prism core. Locates the unanchored comment regex before the property/string rules; suggests reordering. | [Report and citations](trajectory/agents/code/report.json), [tool records](trajectory/agents/code/) |
| Browser Agent, 3 calls | Loads the real Prism core and JSON grammar in Chromium. Reproduces the issue in two scenes and checks genuine line/block comments. | [Report](trajectory/agents/browser/report.json), [final base observation](trajectory/agents/browser/turn_02/tool_01/browser/observation.json) |
| Patch generation | Receives both reports and their observations. Produces five candidates; candidate 01 is selected. | [Generator input](trajectory/S5_input_payload.json), [candidates](trajectory/candidates/), [selection](trajectory/S6_selection.json) |
| Frozen replay | Reuses the final scene and completed actions on the selected patch, with zero model calls. | [Plan](trajectory/S3b/replay_plan.json), [replay status](trajectory/S3b/replay_status.json), [patched observation](trajectory/candidates/01/validation/replay/T2/replay/observation.json) |
| Official outcome | The exported evaluation row records a pass for this patch. | [Official result](official-result.json) |

The agents run in the fixed order Code → Browser, with separate contexts and the same initial hypotheses. The Browser Agent does not read or revise the Code Agent's report. The patch generator is where their evidence comes together.

## What the browser changes about the explanation

An isolated comment regex does match `/*` inside a quoted value. Source inspection also confirms that the comment rule comes first. However, the **composed tokenizer emits no comment token for the issue input**. The initial prediction that a comment token swallows the following members is contradicted by the token stream.

The final member instead has these recorded tokens:

| State | First token | Second token | Third token |
| --- | --- | --- | --- |
| Base | `text: "C` | `string: ":"` | `text: C"` |
| Selected patch | `property: "C"` | `operator: :` | `string: "C"` |

This distinction matters: finding a suspicious regex does not establish the final runtime mechanism. Execution identifies the exact damaged region and adds a concrete preservation requirement: `// tail` and `/* block */` must remain comment tokens. Simply deleting comment support would miss that requirement.

## What the patch and replay establish

The [selected patch](patch.diff) edits only `components/prism-json.js`. It places property/string rules before the comment rule, adds a prefix condition with `lookbehind: true` to the property/string patterns, and makes the comment rule a greedy pattern object. The frozen replay then records the correct property/operator/string sequence for the last member while retaining both genuine comment controls.

The PDF colors are drawn from the recorded token types; they are not screenshots of the replay. The [figure provenance](figure-provenance.json) describes this rendering choice.

The archived [validation](trajectory/candidates/01/validation.json) also preserves its limits: `probe_improved` and `target_improved` are false, and symbol validation is `UNKNOWN`. The DOM token sequence demonstrates the observed repair, but the scalar probe checks do not certify that improvement automatically. Likewise, valid evidence citations mean that a report's references resolve; they do not make every interpretation a proved cause. The [official result](official-result.json) records an evaluated pass, while its separate strict audit remains `unconfirmed`.

## Archive paths

Machine-specific host roots are replaced with `<your_path>/run` and `<your_path>/runtime`. Resolve archived files through the relative paths in [manifest.json](manifest.json). File and payload hashes describe the anonymized records; timestamps and usage metrics describe the original run. The figure's evidence file also records hashes for comparison CSVs used in the manuscript. Those comparison datasets and baseline implementations are outside this release; only the T2Repair DeepSeek result row is exported here. Comments inside source excerpts and generated scene scripts remain intact as experimental evidence.
