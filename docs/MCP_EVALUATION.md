# Evaluating the external-model MCP workflow

The frontier model is the **client**. Grant Tool supplies evidence, versioned
state and review records through MCP. The server never runs the evaluation
model, and this evaluator does not turn model judgments into verified science.

Three invented cases cover plant trays, classroom allocation and sensor batches.
Each asks for a first draft, then supplies a resource or endpoint change requiring
a consistent revision. They exercise ownership, proposed-versus-completed work,
independent units, feasibility and interpretation. These cases are independent
of the earlier evaluation fixtures. Once public, they cannot be assumed to be
permanently unseen by models or developers.

## Offline preparation and protocol replay

Install the MCP extra, then create a new output directory whose parent exists:

```sh
mkdir -p .local/evaluations
python3 scripts/evaluate_mcp_workflow.py prepare --output-dir .local/evaluations/paired-prepared
python3 scripts/evaluate_mcp_workflow.py replay --output-dir .local/evaluations/paired-replay
```

Preparation makes no model, network or authentication request. Replay uses a
scripted responder and the actual MCP client/server protocol. It tests saving,
revision, recorded decisions and the review queue. It is labeled `injected` and
is **not evidence of writing quality**. Neither command opens your archive,
private configuration, existing proposal database or real grants.

## Run an external frontier-model client

Supply the model explicitly through a trusted host-side adapter. The adapter
receives one JSON request on stdin and writes one JSON response to stdout. Its
argv array lives in a separate local JSON file; no shell interpolation is used.
For example, the file can contain `["python3", "/absolute/path/to/client.py"]`.
Then run a freshly prepared evaluation:

```sh
python3 scripts/evaluate_mcp_workflow.py run \
  --output-dir .local/evaluations/paired-prepared \
  --adapter-json .local/client-argv.json \
  --requested-model MODEL_ID
```

The adapter is an explicitly chosen program with the account's ordinary model
access and associated costs. It runs outside the MCP server. Keep it isolated
from the evaluator's rubric, unblinding key and repository files: send only the
provided JSON request to its model, disable browsing and unrelated tools, and
use the listed grant-tool schemas for function selection. The command adapter's
working directory starts empty, but this is not an operating-system sandbox.

Python callers can use the same runner directly:

```python
import asyncio
from scripts.evaluate_mcp_workflow import run_evaluation

# responder(request) makes the external model call using request["messages"],
# request["tools"], request["requested_model"] and request["response_schema"].
summary = asyncio.run(run_evaluation(
    ".local/evaluations/paired-prepared", responder=responder,
    mode="external_model", requested_model="MODEL_ID",
))
```

Each response contains either final `text` or a nonempty `tool_calls` list. A tool
call has `id`, `name` and `arguments_json` (a JSON object encoded as a string).
The supplied `RESPONSE_SCHEMA` supports strict structured output. The host may
return an envelope with `response` holding that schema-shaped value, plus:

- `observed_model`: provider-reported identity; leave absent/null if unavailable.
- `usage`: actual `input_tokens` and `output_tokens`, when reported.
- `cost_usd`: known cost for this call, otherwise absent/null.

Requested identity is recorded separately and never substituted for observed
identity. Missing usage and cost stay explicitly incomplete. Repeated model
calls and tool results are retained, with no silent transport retry. A failure
marks the run failed and preserves its completed calls; use a fresh directory
for another attempt.

The summary updates host responder attempts and returned responses separately.
A responder timeout can leave inference completion unknown; it is not reported
as zero attempts or a successful completion. In injected replay, these counters
count scripted responses, not actual model inference. Per-stage `tool-trace.json`
retains model-requested tools and evaluator verification calls, labeled by origin;
setup calls have their own trace. The tool-call ceiling applies to model requests.

## Comparison design and outputs

Both arms receive the **exact same fictional source objects and scientific
tasks**. The plain arm has no tools. The MCP arm receives the tool schemas and
proposal state, then chooses its own tool calls. Setup uses MCP to preload the
same source packet, without providing additional scientific evidence. The
second-stage investigator update is identical; each arm revises its own first
output. Arm order alternates across cases and is retained in the results.

The default ceiling is eight model turns and twenty model-requested tool calls
per stage. The actual number of calls, elapsed time, reported tokens and reported
costs are recorded separately. Baseline and MCP can use different amounts of
compute within that ceiling. This evaluates the whole workflow; it is not an
isolated equal-compute causal test of MCP. Use the same requested/observed model
and decoding settings for both arms, and report any deviation. Model-request
receipts retain the exact prompts and schemas.

`results.json` retains both arms and their transcripts. `reviewer_packet.json`
contains randomly labeled outputs, the original brief and task, evidence and
case-specific criteria, with rating fields left empty. Revision items also carry
the exact first-stage parent draft and investigator change request, so reviewers
can assess fidelity and continuing task requirements. It contains no arm label,
tool trace or model identity. `unblinding_key.json` is separate: do not give it
to reviewers until their ratings are locked. Traces and tool metadata are
excluded from the reviewer packet, although writing style can still reveal the
arm. Reviewer criteria are never serialized into model requests.

Have independent domain reviewers count unsupported claims, ownership errors,
design errors and revision inconsistencies, and measure editing time. Record
reviewer identities, disagreements and adjudication separately. Mechanical word
counts and unknown citation IDs are sanity checks only. Three fictional cases
are a pilot, not a funding-outcome study or a general superiority claim. Until
independent review is complete, the summary states `quality_advantage:
not_established` even when every protocol and software check passes.

For a completed run created before reviewer-context version 2, enrich the derived
review packet without rerunning a model or changing its outputs:

```sh
python3 scripts/evaluate_mcp_workflow.py enrich-review --output-dir .local/evaluations/completed-run
```

This refuses active/failed runs, verifies each blinded output against its exact
saved result, and adds brief/task/parent/change context. The original run summary,
model outputs, result records and call receipts remain unchanged. A separate
`reviewer-enrichment-*.json` receipt records the original run-code hash and the
current enrichment-helper hash, preserving the distinction when code changed
after model execution.

## Initial live workflow pilot — 2026-10-31

One external-model run completed all three fictional cases, with a draft and a
revision in each arm. Both arms received identical scientific inputs per case.
The requested model was `gpt-6-astra` throughout; the adapter did not report the
observed model identity. There were no server-side model calls.

| Completed-run measurement | Plain model | MCP workflow |
| --- | ---: | ---: |
| Draft/revision outputs | 6 | 6 |
| External model calls | 6 | 27 |
| Reported input tokens | 53,630 | 416,936 |
| Reported output tokens | 2,126 | 6,765 |
| Outputs containing unknown citation IDs | 0 | 0 |

All six MCP outputs exactly matched their saved versions. All three revision
updates were recorded, and the model read the review queue after each save.
These are workflow checks, not judgments of evidence support. This run exercised
state retrieval, saving, decisions and review queues; it did not exercise public
paper retrieval or claim-assessment mutations. Those have separate software
tests.

The tool workflow used substantially more calls and input tokens. Token totals
include repeated context and tool results; they are not billable-cost estimates.
Cost was unavailable. An earlier adapter failure and separate diagnostic calls
are excluded from this completed-run table and retained locally. The synthetic
fixtures are small and public, there was only one run, and independent domain
review is pending. **No writing-quality advantage is established.**

The run used fixture SHA-256
`5bcc9d96baeb38e7f86833c4071e6e12b18338c7961ec3185659b124e00b94dc`
and runner SHA-256
`b8433a1d5870d71a1cdf0eb7801124a39ce9e51d526c2a480c13ce9aae47fcef`.
The reviewer-context enrichment described above was applied after completion,
with a separate receipt; original outputs and call records were unchanged.
This aggregate report does not publish the local execution logs.
