# Internal harness and maintenance reference

Grant Tool's supported user workflow is [stdio MCP](MCP.md). The connected frontier model reads tool results, reasons, drafts, and reviews; the server never invokes a model. This page documents shared Python/state machinery and local maintenance for contributors. It is not a second user-facing application or a server-generation setup guide.

## Shared state

`GrantHarness` takes the configured `library.sqlite3` path; proposal state lives in `studio.sqlite3` beside it. `GrantHarness.from_config(path)` resolves the source configuration. Construction opens state without an inference request. MCP supplies an explicit inference-forbidden callback and replaces a supplied harness's generator with the same boundary. No generation tool or flag is registered.

A model-independent development inspection can use an explicit forbidden provider:

```python
from grantlib.harness import GrantHarness

def no_inference(*args, **kwargs):
    raise RuntimeError("This operation must not invoke a model")

with GrantHarness.from_config("config.local.json", provider=no_inference) as harness:
    for proposal in harness.list():
        print(proposal["id"], proposal["title"])
```

Use the context manager or close a harness when finished. Its service owns active jobs; shutdown requests cancellation of those jobs rather than detaching them.

## Proposal operations used by MCP

| Python method | Purpose |
| --- | --- |
| `list()`, `get(proposal_id)`, `artifact(proposal_id, artifact_id)` | Read proposal state and an exact saved version |
| `create(brief)` | Create a proposal with required `title` and `question` |
| `update(proposal_id, brief, expected_revision=N)` | Replace the brief using the inspected revision |
| `decide(proposal_id, text, expected_revision=N)` | Record an actual attributed investigator instruction |
| `save(proposal_id, markdown, expected_revision=N, base_artifact_id=ID, author="assistant")` | Save caller-authored text as a new child version |
| `start(proposal_id, "evidence", expected_revision=N)` | Start a local evidence job without inference |
| `job(job_id)`, `wait(job_id, timeout=SECONDS)`, `cancel(job_id)` | Observe or cancel that same job |
| `export(proposal_id, artifact_id, format="md")` | Return Markdown text, or Word bytes for `docx` |

Optional brief fields are `mechanism`, `funder`, `opportunity_url`, `due_date`, `language`, `constraints`, `notes`, `keywords`, and `projects`. Preserve unknown inputs rather than filling them from assumptions. `projects` contains exact configured labels; an empty selection searches broadly.

Read the new revision after a mutation. A conflict requires reconciling the intended change. Saving uses the explicit parent's evidence snapshot and can retain explicitly cited new reference/paragraph sources without changing that parent. The saved origin is `assistant_import`; citation checks and word counts do not constitute a scientific assessment. Imported versions do not inherit a parent's automatic model check or claim judgments.

`wait` observes an existing job. A timeout does not start or cancel another job; retain the owner's process and inspect the same ID. Evidence completion has no generated draft artifact. Job recovery requires evidence that its recorded owner is missing.

## Evidence and claim operations

| Python method | Purpose |
| --- | --- |
| `resolve_reference(proposal_id, identifier, expected_revision=N)` | Resolve one explicit DOI or PMID into a retained metadata/abstract snapshot |
| `fetch_full_text(proposal_id, pmcid, expected_revision=N)` | Retrieve one eligible public article snapshot |
| `full_text(proposal_id, document_id)` | Read extracted paragraphs and provenance |
| `retain_passage(proposal_id, document_id, paragraph_id, expected_revision=N, actor="assistant")` | Retain an exact stored paragraph without accepting replacement source text |
| `add_claim(proposal_id, artifact_id, quote, expected_revision=N)` | Register an exact quotation from the selected saved draft |
| `assess_claim(proposal_id, artifact_id, claim_id, expected_revision=N, **judgment)` | Append a version-specific judgment |
| `review_queue(proposal_id, artifact_id, offset=0, limit=20)` | Read bounded deterministic triage without mutation or inference |

Judgments use `verdict`, `source_id`, `excerpt`, `locator`, `note`, and `reviewer`. Support/partial/contradiction needs an exact retained excerpt, locator, and explanation; unresolved still needs an explanation. The MCP wrapper fixes `reviewer="assistant"`. Citation membership, quotation matching, and reading scope are enforced independently from the caller's scientific reasoning.

The queue includes registered claims and conservative uncovered prose candidates. `registration_required` indicates that a candidate must be registered before it can receive a judgment. The page reports exact text/spans, current assessments, source scopes, and explicitly qualified parent matches. Parent judgments are historical only. Empty results are not a statement of scientific completeness or correctness.

## Memory boundary

The store provides `memory_catalog`, `create_memory`, `revise_memory`, and `select_memory`. MCP exposes catalog reads, assistant proposals, and selection of exact already-confirmed versions only. Proposed, retired, stale, or review-due records cannot be selected as confirmed input. A proposal selection is not confirmation.

Record fields include `category`, `text`, `scope`, `provenance_note`, optional `tags` and `review_after`, and status. Assistant writes remain proposed. The trusted local memory maintenance commands can record actual investigator confirmation, but the caller must not use a tool argument or shell workaround to impersonate that authority. This boundary does not prevent ordinary proposal work with attributed instructions and explicit unknowns.

## Local maintenance

Normal users can complete intake and indexing in their MCP conversation. These commands remain useful for local diagnostics and explicit maintenance:

```sh
python -m grantlib --config config.local.json stats
python -m grantlib --config config.local.json inventory
python -m grantlib --config config.local.json search 'primary endpoint' --role all
python -m grantlib --config config.local.json proposal list
python -m grantlib --config config.local.json refresh-status
```

`ingest` reads only configured sources and writes generated state to the separate data directory. Do not index `.local/` proposal data as original research. Use [refresh setup](REFRESH.md) for bounded public-topic maintenance; it does not invoke models. Explicit curation persists ownership/evidence-status corrections separately from extraction.

## Developer evaluation machinery

The repository retains internal provider adapters, generation/check/repair pipelines, and legacy CLI actions for controlled synthetic research and regression tests. These are not exposed through MCP and are not required for its installation or use. Direct developer invocation can make online inference requests and therefore has a different execution/data boundary.

The legacy `GrantHarness` default provider is `CodexProvider`; contributors performing model-independent work should pass a forbidden or injected test provider explicitly. Provider wrappers validate schemas, bounded completions, cancellation, and recorded metadata; they cannot certify scientific support. Do not advertise the legacy `proposal run` path as the product workflow or reintroduce it as an MCP tool.

For the current comparison of a frontier-model caller using MCP versus a plain-model baseline, use [MCP evaluation](MCP_EVALUATION.md). [Testing and evaluation limits](ASSISTANT_DEVELOPMENT.md) distinguish preparation, mocked execution, actual inference, and independent scientific review. Historical developer scripts are documented in [scripts/README.md](../scripts/README.md).
