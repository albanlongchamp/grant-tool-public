# Grant Tool

An MCP server for developing research grants with traceable evidence and versioned drafts.

**The frontier model in your conversation does the reasoning, writing, and review.** It calls Grant Tool to inspect your archive, retain public evidence, record decisions and claim judgments, and save or export a selected version. The server never starts another model. There is no browser UI, website, or HTTP service.

```text
Researcher ↔ frontier-model assistant ↔ Grant Tool (stdio MCP) ↔ local research workspace
```

## What the tools preserve

- Exact source passages, locators, ownership labels, and proposed-versus-completed status.
- Proposal briefs, investigator instructions, explicit parent versions, and each draft's evidence snapshot.
- Public reference identities, accessible full-text paragraphs, and version-specific claim judgments.
- A bounded evidence-review queue showing unreviewed, disputed, or changed text for the assistant to inspect.
- Editable Word or Markdown exports with a styled reference list, source notes, and unresolved inputs.
- A shared reference library harvested from the papers your previous applications cited, plus PubMed discovery, PMID/DOI resolution, CSL-JSON records, and RIS export.

This is experimental research software. A retained source, an assistant judgment, or an empty review queue does not establish scientific validity, funding eligibility, or submission readiness.

## Install and connect

Use Python 3.11 or newer with SQLite FTS5 support:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
```

Configure your MCP client to launch the installed server with an absolute interpreter and workspace configuration path. For clients using the `mcpServers` format:

```json
{
  "mcpServers": {
    "grants": {
      "command": "/absolute/path/to/grant-tool-public/.venv/bin/python",
      "args": ["-m", "grantlib.mcp_server", "--config", "/absolute/path/to/workspace/config.local.json"]
    }
  }
}
```

The workspace configuration may be absent on first launch. Once connected, ask:

> Use the grants tools to help me develop my proposal. Start by checking the workspace and existing proposals.

`grants_status` supplies one first-use question: where your prior applications are, which RFP or funding call to use, and the known funder, mechanism, and deadline. No prior applications is a valid answer, and unknown details can wait. The assistant saves your supplied answers with `grants_setup_workspace`; indexing reads only explicitly configured folders. See [MCP setup and workflow](docs/MCP.md).

PDF extraction needs Poppler's `pdftotext`; legacy Word and RTF extraction use macOS `textutil`. Extraction gaps remain visible. The server does not perform OCR or interpret figures.

## The conversation workflow

1. Inspect workspace status and reuse the intended proposal.
2. Read the funding call and gather local evidence, retaining ownership and uncertainty.
3. Find candidate papers on PubMed (with NIH iCite influence metrics), resolve the chosen ones by PMID or DOI, read exact paragraphs, and explicitly retain the passages used.
4. Let the connected model draft, then save a new version with its evidence citations.
5. Read the review queue, inspect consequential claims and their sources, and record assistant judgments.
6. Record actual investigator changes, revise the selected parent, re-check cited references for retractions, and export the chosen saved version in a reference style (Vancouver/NLM, Nature, Science, Cell, NEJM, JAMA, APA, Lancet, or JCI) with a companion RIS file for EndNote or Zotero.

The assistant can perform this loop through MCP. Citation checks validate identifiers; exact quotation checks validate location; the model and investigator evaluate scientific support. Previous support judgments never silently transfer to a revised draft.

## Try invented data

```sh
python scripts/demo.py
```

This prepares a fictional workspace and prewritten draft without network or model requests. Point your MCP client's `--config` argument at the absolute path to `.local/demo-mcp/config.local.json`, then ask it to inspect the fictional proposal and review queue. Any subsequent reasoning is performed by your connected assistant. See [the demo](examples/demo/README.md).

## Data boundaries

Your archive, private configuration, indexes, proposals, and exports stay outside the published source tree. Returned MCP content enters the connected assistant's context and is subject to that service's policies. Local storage does not make the conversation offline or encrypted. Read [data handling](PRIVACY.md) before connecting real research.

Public searches use explicit public terms or identifiers. NIH award descriptions are funded plans rather than verified results. Publication discovery is separate from retained evidence. Stored guidance must be checked against the current applicable opportunity. Search is lexical. Research topics and synonyms are not built in: define your own under `vocabulary` in the workspace configuration (see `config.example.json`). Coverage is not comprehensive across disciplines or funders.

## Documentation and development

- [MCP tools, setup, and full workflow](docs/MCP.md)
- [Architecture and evidence boundaries](docs/ARCHITECTURE.md)
- [Internal Python and maintenance reference](docs/HARNESS.md)
- [Optional public-source refresh](docs/REFRESH.md)
- [Grant Architect instructions for the calling assistant](skills/grant-architect/SKILL.md)
- [Software testing and evaluation limits](docs/ASSISTANT_DEVELOPMENT.md)
- [MCP comparative evaluation](docs/MCP_EVALUATION.md)
- [Contributing](CONTRIBUTING.md)

Run `python -m unittest discover -s tests -v` for synthetic software tests. Their results establish the behaviors exercised, not superior proposal quality or funding outcomes.

Released under the [MIT License](LICENSE).
