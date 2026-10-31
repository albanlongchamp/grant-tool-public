# Data handling

This repository distributes software, public guidance summaries, and invented examples. It does not include a researcher's grant archive, saved proposals, private configuration, credentials, or private development history.

## Where information goes

| Operation | Data handling |
| --- | --- |
| First-use setup | Saves supplied folder paths, funding-call reference, and known submission details in private configuration. A supplied local RFP is extracted into a private snapshot; URL intake does not fetch the URL. |
| Archive indexing and search | Reads explicitly configured folders without changing originals; extraction caches and indexes go into the configured data directory. No home-directory discovery is performed. |
| Proposal versions and exports | Stores briefs, evidence, drafts, decisions, judgments, and exports locally. These may contain confidential research and source paths. |
| MCP tools | Returned passages and drafts enter the connected assistant's context. Its service and retention policies apply. The MCP server never starts another model. |
| Public-source retrieval | Sends explicit public identifiers, official URLs, or configured public search terms to the relevant service. Do not put confidential aims or unpublished prose in these inputs. |
| Refresh scheduling | Runs only when configured and installed. Public maintenance does not require model inference. |

The product is a stdio MCP server, with no browser UI or public HTTP endpoint. An authenticated SSH connection can place the server on a shared host; see [MCP setup](docs/MCP.md). Local storage does not imply encryption or offline reasoning. The model remains in the calling client.

Accessible article snapshots and deliberately retained paragraphs are separate. A paper download does not automatically add all its text to a proposal. Reading a paragraph through MCP still transmits that returned text to the connected assistant. Historical drafts retain the exact evidence selected for those versions.

Research-memory proposals and assistant claim judgments are not investigator-authenticated facts. MCP cannot confirm or retire memory on behalf of an investigator. Already-confirmed records can be selected only at their exact current versions and within their recorded scope.

## Keep research outside Git

Use `config.local.json` and the ignored `.local/` directory for private state. Keep source documents outside the checkout or in ignored `source-documents/`. The demo uses a separate ignored data directory and only fictional source files. If you choose other locations, add their paths to your own ignore rules.

Before sharing changes, inspect `git diff --cached` and run `python scripts/check_public_release.py --history` from a complete clone. This checks staged files, reachable commit contents (including deleted files), and annotated tag metadata for selected private artifacts and credential/path patterns. CI fetches full history and runs this check. The checker cannot establish that all prose is anonymous or every secret removed. Review names, affiliations, grant titles, unpublished ideas, document metadata, and GitHub logs and attachments separately. Ignore rules do not remove files already committed.

Publishing this repository does not publish ignored workspace files or open a connection to the publisher's computer. Example installation paths and SSH host names are placeholders. GitHub ownership, activity, and account profiles remain publicly attributable even when source files and commit author fields contain no personal details.

Use invented reproductions in public issues and pull requests. Do not attach actual proposals, participant data, private logs, or configuration. Developer evaluation utilities that explicitly invoke models are separate from the MCP service; their inputs and outputs also require appropriate handling.
