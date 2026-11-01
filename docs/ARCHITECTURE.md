# Architecture and evidence boundaries

## MCP execution boundary

The public product is a stdio MCP server. The frontier model in the connected client performs reasoning, writing, and review. `mcp_server.create_server` installs an inference-forbidden callback, including when a Python harness is supplied. Tool registration exposes local state operations and explicit public lookups, with no model-generation tool, model-selection flag, UI, website, or HTTP service.

Tool results are bounded and enter the connected model's context. The server validates identifiers, exact quotations, source ownership scope, proposal revisions, and result sizes; it does not establish semantic truth. Public network tools accept explicit identifiers or official URLs. Read-only tools do not silently refresh sources or mutate proposal state.

## Workspace intake and archive

A new connection can start before `config.local.json` exists. `onboarding.py` reports one minimal intake question through status. `grants_setup_workspace` saves supplied source folders and funding-call/submission details at the fixed configuration path using a config hash for optimistic concurrency. Unknown details and no prior archive are valid. The server never searches the home directory for research files.

Archive indexing is a separate explicit action over configured roots. A supplied local RFP is extracted to a private hashed snapshot, while a URL is stored without being fetched by intake. RFP reading returns bounded windows of that snapshot; it is funding context, not research evidence. Current official-page verification remains a separate operation.

`library.sqlite3` stores document inventory, exact passage units, locators, candidate labels, observed bibliographic identifiers, and an FTS5 index. Source files remain unchanged. Document identity incorporates named root and relative path; passage identity also incorporates locator and content. Duplicate text is not independent scientific evidence.

DOCX extraction uses standard-library OOXML parsing; PDFs require Poppler text extraction; legacy Word/RTF use macOS `textutil`. Cached extraction is keyed by content hash, format, and extraction version. Symlinks are inventoried rather than followed. Missing drives, traversal failures, and extraction problems remain visible. Scanned pages, figures, and complex layout are not interpreted.

Explicit corrections in `curation.json` are separate from generated extraction and reapplied during rebuilding. Automatic topics, ownership, and evidence-status tags are retrieval aids. No research topics are built in; the optional `vocabulary` configuration supplies topic patterns and synonym groups, which are recorded in the library snapshot. `retrieval.build_packet` expands local lexical queries with those aliases, ranks substantive passages, limits source concentration, and bounds the retained context. It does not perform semantic embedding search or verify the claims it retrieves.

## Public discovery and guidance

`nih.sqlite3` preserves imported public application descriptions, annual records, core project numbers, and request/file provenance. Explicit public-topic queries and bounded ExPORTER imports stay separate from private text. Capped or failed collections remain visibly incomplete. Funded descriptions are plans and context, not completed results, full applications, or a comparison against rejected grants.

`guidance_sources.json` contains dated paraphrases of selected official sources. It records applicability and limitations; it does not establish current compliance. `grants_verify_official_source` retains the actual fetched official page with a timestamp/hash. Later windows read that same snapshot. The calling model must interpret applicable requirements and resolve failed or unsupported retrievals.

The refreshed PubMed discovery store contains metadata, abstracts, notices, and collection freshness. Searching it does not automatically retain publication evidence. Refreshes do not replace source versions inside saved proposal artifacts.

## Proposal versions, citations, and jobs

`studio.sqlite3` holds proposal briefs, evidence packets, artifacts, decisions, judgments, and memory separately from the archive. Mutations use optimistic proposal revisions. Each artifact records its explicit parent and evidence context. Saving a new version does not rewrite the parent's draft, source packet, or claim judgments.

The connected model writes the Markdown passed to `grants_save_draft`. The server records `assistant_import`, validates source IDs, computes word counts, and retains applicable source notes. It does not generate prose or run an automatic scientific assessment on that save. Drafting instructions and reviewer judgments remain attributed content, not server-certified facts.

Evidence gathering uses durable queued/running/completed/failed/cancelled jobs. The MCP process owns its active work. Observation timeout does not create another job; recovery requires evidence that the recorded owner has disappeared. Shutdown requests cancellation of owned work. Historical generation metadata remains readable for old/developer artifacts without changing the current no-inference product boundary.

## Reference library and citation styles

`publication_search.search` queries PubMed live (esearch, efetch) for explicit public terms and enriches the returned PMIDs with NIH iCite influence metrics; failures of the metrics call degrade to a warning. `references.resolve` snapshots carry structured author names, journal abbreviation, volume, issue, and pages. `bibliography` converts snapshots to CSL-JSON, substitutes draft markers (numbered, superscript, or author-year), renders the built-in styles, and writes RIS. No citation-processor package or style download is involved; the style set is fixed in code.

`reference_library` keeps one shared record per paper in the proposal database, with the archived documents that cited it (`library_citations`), the proposals that adopted it (`library_proposal_links`), and remembered failed lookups (`library_attempts`). Harvesting reads the archive index's observed identifiers and reference-list sections; fuzzy Crossref matches are stored with their confidence and never upgraded silently.
`reference_library.recheck` re-resolves stale references and records notice flags in `reference_checks`, keyed by PMID or DOI, so exports can mark retracted or corrected entries without rewriting any snapshot.

## Publication reading and explicit retention

`references.resolve` accepts one explicit DOI or PMID and uses fixed public endpoints with bounded requests, identity checks, and throttling. No grant passage accompanies a lookup. A retained bibliographic/abstract record establishes identity and reading scope, not scientific support.

`publication_text.fetch` accepts an explicit PMCID and selects an accessible open-access article version through official metadata and JATS retrieval. Identity, license basis, checksums, extraction coverage, and notices are retained. Unsupported/omitted material stays explicit. A source may be useful only after the caller has checked the relevant methods, results, and limitations.

`publication_store` holds immutable proposal-owned article snapshots and retained paragraphs. List operations omit whole-article text. Readers expose bounded paragraph windows with IDs, hashes, and locators. Retention derives the exact whole paragraph from the stored snapshot, records the assistant actor, and returns an `F_` source; the client cannot substitute source text. Reading, retention, and scientific assessment are separate steps.

Newly cited reference or paragraph records enter the new artifact's context. Earlier artifacts keep their old snapshots. Unselected article paragraphs are not automatically evidence for the draft. Metadata/abstract and full-text-excerpt scopes remain distinct in judgments and exports.

## Claims and evidence-review queue

`manual_claims` registers exact saved-draft quotations. `claim_assessments` appends judgments with reviewer, date, exact source excerpt, locator, reading scope, snapshot/hash, and explanation. Claim identities incorporate the artifact. MCP fixes the reviewer to assistant; it cannot assert investigator identity. Support, partial support, and contradiction require source text and a locator. An unresolved record still needs an explanation.

`claim_review.review_queue` is deterministic, read-only triage. It combines existing claims with conservative prose candidates from uncovered paragraphs. It reports current disputed/unreviewed judgments, missing retained source IDs, unlocated structured text, and parent changes. Exact offsets and explicitly bounded text let the caller inspect the original. Similar-wording parent candidates are lexical aids only. Older judgments are never inherited as current support.

The queue does not decide which scientific claims are consequential or which proposed statements require citations. It is not exhaustive claim detection. An empty queue or a support judgment cannot certify correctness. The connected model reads the actual draft and sources, assesses important claims, and revises the selected parent in conversation.

## Investigator decisions and reusable memory

Recorded decisions preserve an attributed investigator instruction and its proposal/date scope. Their citation IDs remain separate from scientific findings. The caller must not reclassify its own recommendation as an investigator decision; the server does not authenticate the speaker from text alone.

Memory uses append-only versions, a catalog revision, and exact proposal bindings. Records retain actor, status, text, scope, provenance, confirmation time, and an optional review date. MCP can propose records and select already-confirmed, current, non-due versions. It cannot confirm or retire a record. Trusted investigator maintenance is outside the model tool channel.

Changed selections require fresh gathering for current evidence. Historical `M_` citations remain dated snapshots rather than claims about current capability. Stale or review-due entries do not become confirmed because the model selected them. Ordinary work can proceed without confirmed memory by retaining decisions and explicit unknowns.

## Export and evaluation

`docx_export` creates editable WordprocessingML with draft text, source notes, unresolved inputs, and attributed judgments using the standard library. Markdown and Word exports preserve the selected artifact. Unsupported figure/equation markup fails explicitly. The layout is generic; sponsor formatting and rendered-document inspection remain separate work.

Developer-only provider and generation/check/repair modules remain for synthetic experiments and regression coverage. They are not MCP execution paths. The comparative evaluation exercises a frontier-model caller with MCP and a plain-model baseline using controlled fictional inputs. Software tests establish exercised mechanics; model quality, reviewer agreement, editing time, and cost require actual measured evaluation. See [evaluation boundaries](ASSISTANT_DEVELOPMENT.md) and [MCP evaluation](MCP_EVALUATION.md).
