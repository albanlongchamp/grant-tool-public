# Testing and evaluation limits

Grant Tool preserves traceable evidence, exact proposal versions, attributed judgments, and explicit investigator inputs. Its product execution model is a frontier-model assistant calling a stdio MCP server. The server never calls a model and supplies no UI or website.

## Software verification

```sh
python -m unittest discover -s tests -v
```

Tests use invented research content, isolated temporary stores, injected responses, and mocked public services. Relevant coverage includes MCP tool registration and no-inference enforcement, intake paths and revisions, reference/full-text scope, exact claim quotes, assistant attribution, memory boundaries, read-only review queues, immutable evidence, branch selection, cancellation, and exports. Format-specific tests may require optional system utilities; inspect skipped-test reasons.

A passing suite establishes the behavior exercised for that revision and environment. It does not establish scientific accuracy, grant quality, funding success, or superiority to another tool. In particular, quotation matching verifies a text location, citation validation verifies source membership, and queue rules identify review candidates. None determines semantic support.

## Evaluate the calling model's workflow

Use [MCP comparative evaluation](MCP_EVALUATION.md) for the current evaluation protocol. Compare the same frontier model on the same fictional scientific inputs with and without MCP. Preserve exact inputs/outputs, tool traces, model configuration, revisions, evidence access, and resource budgets. Disclose differences in call count, token use, wall time, and cost rather than attributing an extra inference budget to tool quality.

Measure unsupported claims, ownership mistakes, scientific design errors, revision consistency, and investigator editing effort. Blinded domain review should assess the complete outputs using held-out cases; report reviewer agreement and uncertainty. Automated scores and injected mock runs are separate from independent scientific judgments.

Preparation creates evaluation material; it is not a model run. A scripted or injected client can test protocol mechanics without demonstrating better grant writing. Actual model comparisons and blinded reviews must be reported only when performed. Preserve failures and unchanged outputs rather than silently repairing benchmark artifacts.

## Legacy developer experiments

Historical provider adapters, generation/check/repair pipelines, and scripts remain for controlled synthetic development. These can explicitly invoke a model when run by a developer; they are not exposed by the MCP server and do not represent the product's caller-model architecture. Their fixture-only preparation and opt-in execution are documented in [scripts/README.md](../scripts/README.md) and the [internal harness reference](HARNESS.md).

Their schema, quotation, cancellation, and bounded-repair tests are useful regression evidence. They do not establish that the connected model follows instructions or that a critique is scientifically correct. An old artifact's automatic assessment remains attributed historical metadata; saving a new MCP draft does not inherit it.

## Review substantive outputs

An investigator and the calling model should inspect the actual draft and its material inputs:

- Preserve the intended question, scientific direction, language, resources, and consequential constraints.
- Read primary sources for important claims, retaining proposed/completed status, ownership, model, endpoint, quantities, and uncertainty.
- Check allocation and independent experimental units, pairing/nesting, controls, feasibility, aim dependencies, and null-result interpretation.
- Compare a revision with its exact parent and investigator change, including effects elsewhere in the argument.
- Check current applicable funding requirements against their actual official sources; a retained snapshot alone is not compliance verification.
- Inspect the selected export and unresolved inputs before delivery. A generic Word file is not sponsor-formatting approval.

Keep real archives, configurations, proposals, model requests/responses, and execution logs private. Only invented cases belong in committed tests, examples, or public evaluation records. See [data handling](../PRIVACY.md).
