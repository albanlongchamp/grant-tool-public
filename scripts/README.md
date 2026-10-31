# Synthetic assistant evaluation

`evaluate_assistant.py` uses only the committed invented case in
`tests/fixtures/proposal_workflow.json`. It never opens the application archive,
NIH database, local configuration, or real grant files. The output directory must
be new, with an existing parent. It is created with owner-only permissions.

Prepare a fixture snapshot without loading the model backend or making model calls:

```sh
python3 scripts/evaluate_assistant.py --output-dir /tmp/grant-eval-prepared
```

Explicitly run the workflow using the authenticated Codex backend:

```sh
python3 scripts/evaluate_assistant.py --output-dir /tmp/grant-eval-live --run-model
```

Use a fresh output path for every invocation; existing paths are refused. There
are no transport retries or resume operations. `--timeout SECONDS` sets the job
observation budget and caps each model call (default 900); each drafting job's shared
remaining deadline is preserved. Interrupting requests cancellation and saves observed
job states. Actual generation needs the application's Codex authentication and
network access; preparation does not inspect authentication.

The sequence is design → aims → review → recorded investigator change → revision.
It selects the first proposed direction. Revision selects the review, exercising
the server's resolution to the reviewed aims. The unchanged fictional case tests
old proposed work, collaborator ownership, funded plans versus results, paired
organs and repeated measurements, withdrawn RNA-seq resources, and a changed
primary endpoint. The fixture's approximate 600–800-word aims request remains in
its original brief notes. It does not create a structured length target: the
current resolver considers only a drafting task's direct instruction and numeric
controls, never brief notes or evidence sources. Fresh fixtures with supported
whole-draft word bounds in their task instructions use the ordinary production
resolver and record its provenance.

Four user-level jobs make **7–13 underlying model calls**. Design and aims each
generate a draft and run a fresh single-draft scientific check; review uses one
call; revision generates a draft and runs its before/after check. Each of the
three drafting jobs may add one combined repair and one fresh final check,
for at most four calls per job. The same stage protocol supports strategy, which
is not an additional job in this fixture. Failed attempts count toward the cap.
There is no second repair cycle, and the harness does not disable the production
checks. A failed audit or repair can retain the last valid draft with an explicit
incomplete check. Completion of a job therefore does not mean scientific approval.

Outputs include the fixture and code hashes; exact input text, context, schema,
raw response and observed metadata under `calls/<number>-<action>-<stage>/`;
request and terminal job records; complete version JSON and
Markdown exports; recorded decisions; the isolated `studio.sqlite3`; and
`summary.json` with the observed action/stage/protocol call manifest, timings,
usage summed once across all calls, citation checks, scientific-check status and
word counts. Exact inputs are checked for fictional source provenance before
every generation, audit and repair; recorded decision sources must match the
known invented decision text and its exact metadata. Before/after checks preserve parent
artifacts, the brief and investigator decisions. Counts use the versioned
production convention in `draft_metrics.py`: whitespace tokens containing
letters or digits, excluding citation markers and Markdown-only syntax.
New model outputs separate the body from change notes; legacy boundaries use
a conservative exact-heading rule and report their uncertainty. Existing
evaluation receipts retain their original counting convention. The exact
generated text is never edited.

Citation resolution is not scientific quality or verified claim support. Review
the actual drafts independently, including null-result interpretation, source
ownership, methods feasibility and whether the recorded change propagated.

For offline harness tests, import the script and call `prepare(path)`, then
`run_evaluation(path, generate=synthetic_callable)`. The callable follows
`grantlib.model.generate`'s interface. Such runs are labeled `generation_mode:
injected`; they validate workflow mechanics and do not constitute model evidence.

## Explicit experimental review contract

The default remains the historical quotation contract. To prepare the same
fictional workflow with the experimental defects-only quotation contract:

```sh
python3 scripts/evaluate_assistant.py --output-dir /tmp/grant-defects-prepared --audit-contract scientific_defects_v1
```

Adding `--run-model` makes the same bounded 7–13 requests; it adds no review or
repair opportunity. Preparation records the selected contract and the exact
serialized fixture hash. A later API execution must supply the same
`audit_contract` and unchanged fixture. Historical legacy preparation receipts
remain readable; new-contract receipts require the fixture binding. Every new
audit prompt and schema is reconstructed and checked before provider invocation.
Saved audit metadata records the contract version and exact input/report hashes.

`Service` accepts optional `draft_audit_factory` and `revision_audit_factory`
callables for this controlled integration. Normal Studio callers do not select
them. An invalid audit remains incomplete with the last valid draft retained;
there is no silent fallback to the older contract. Human exports retain scientific
text, exact quotations and source notes; full machine provenance remains in the
saved artifact JSON. Passing injected tests does not establish scientific quality.

## Held-out revision cases

`prepare_revision_cases.py` uses two additional invented cases from
`tests/fixtures/revision_heldout.json`: cell-culture replicate units, batch allocation
and masked readout; and clinic-level randomization, nested observations and
uncertain null results. Each includes a clean parent and a deliberately flawed
candidate. Expected findings are saved separately for reviewers and never enter
the model prompt.

Prepare either case without model execution:

```sh
python3 scripts/prepare_revision_cases.py --case cell_culture_units --output-dir /tmp/cell-case
python3 scripts/prepare_revision_cases.py --case clustered_uncertainty --output-dir /tmp/cluster-case
```

Add `--run-model` with a fresh output path to run the production revision-check
pipeline on the supplied candidate. This injects the initial fictional candidate
instead of generating a draft: an actual audit can trigger one actual repair and
one actual final audit, for **1–3 calls per case**, at most six for both cases.
The runner saves exact per-stage prompts/results, the anchored check, final
artifact, Markdown and Word exports, and unchanged-parent checks. A separate
`revision-seed.json` supports a future evaluation starting from the clean parent.
No actual-model call is made by preparation or by the injected unit tests.
