# Grant Architect MCP workflow

The frontier model in the conversation is responsible for scientific reasoning, writing, review, and choosing tool calls. Grant Tool is a stdio MCP server with no model inference, UI, or website. These instructions describe the caller's workflow; exact tool schemas are listed by the live server and summarized in [MCP documentation](../../../docs/MCP.md).

## Intake and proposal selection

1. Call `grants_status`. If `intake.needs_input` is true, ask its minimal question once and use information already present in the conversation. Accept no prior archive, an unknown RFP, or incomplete submission details. Do not invent paths or scientific capabilities.
2. Save supplied folder paths, an RFP URL/local file, and known funder/mechanism/deadline through `grants_setup_workspace`, using `intake.config_revision` as `expected_config_revision`. Omit fields to preserve previous answers; explicit `[]` or empty strings clear them. An omitted RFP preserves the existing snapshot without rereading.
3. Index only those configured folders through `grants_index_archive`. Read local funding-call snapshots with `grants_read_rfp`. Indexing and intake do not establish ownership, completed findings, or current funding requirements.
4. List proposals and inspect the intended one. Reuse it when it matches the task. If needed, create a brief with `title` and `question`, adding only supplied context and explicitly labeled unknowns.

Workspace intake is separate from the proposal brief. Apply relevant submission details to the intended proposal rather than assuming that intake changed every proposal. An RFP is funding context, not preliminary research evidence.

## Gather and read evidence

Use the proposal revision actually inspected to call `grants_gather_evidence`. Preserve the returned job ID and keep the server owner alive. Observe that same ID with `grants_get_job`; cancellation uses `grants_cancel_job`. A timeout is not permission to start duplicate work. Continue only after the job is terminal, then reread the proposal revision.

Read `grants_get_evidence` with its pagination. For a revision, supply the selected `artifact_id` to inspect that version's historical packet. Search across roles while exploring mixed source folders; default uncertain ownership is not applicant ownership. Read relevant source windows with their original locators and resolve ambiguity before using a consequential claim.

Keep the evidence layers distinct:

- **Archive passages:** may contain plans, unverified reported results, reviews, collaborator material, templates, or literature. Preserve status and ownership.
- **Public awards:** describe funded/proposed projects, not necessarily completed experiments or successful writing techniques.
- **Guidance/RFPs:** supply dated funding context and requirements, not scientific results.
- **Publication discovery:** metadata and abstracts identify candidate sources. Retention and support assessment are separate.
- **Investigator decisions/memory:** attributed instructions or scoped statements, not independent confirmation of scientific findings.

Search results are not automatically retained proposal citations. Missing evidence stays a gap; do not repair it with plausible prose.

## Read and retain publication evidence

When a claim needs literature, call `grants_find_references` with explicit public topic terms (never proposal prose), optionally restricted by year or sorted by `most_cited`. Compare candidates on abstract fit, publication type, retraction flags, and iCite relative citation ratio, and prefer the primary report over a review for a specific finding. Resolve the chosen paper, or an explicit observed PMID/DOI, with `grants_resolve_reference` using the current proposal revision. Do not submit private grant paragraphs as public lookup inputs. Inspect the returned identity, publication notices, limitations, and abstract. `grants_read_reference` continues the retained abstract without replacing it with a newer one.

When full text is needed, call `grants_fetch_full_text` with an explicit PMCID. Read `grants_read_full_text` pages; continue truncated text using `grants_read_publication_paragraph` and the same document/paragraph IDs. Inspect source version, license basis, abstract/body scope, and omitted material. A retrieved article is not evidence that all of it was read.

Use `grants_retain_publication_passage` for the exact relevant paragraph IDs. Retention returns an `F_` citation source, preserves the original text, and records assistant selection. It does not establish support. If essential figures, tables, methods, or supplements are absent, seek the actual material through an authorized route or keep the claim unresolved.

## Develop and save the scientific argument

Preserve the investigator's intended question and requested scope. For substantive design work, establish the unresolved gap, competing explanations, decisive comparison, unit of allocation, independent experimental unit, primary endpoint, analysis logic, feasibility, and interpretation of contrary/null findings. Repeated measurements are not independent replication. An imprecise null is not equivalence. An available assay is not evidence that an experiment was completed.

Write in the current conversation. Cite exact retained source IDs such as `[[P_...]]`, `[[N_...]]`, `[[G_...]]`, `[[R_...]]`, or `[[F_...]]` where they actually support the statement. Do not infer semantic support from a valid ID. Literature citations found in an old proposal need their own source review.

Save the draft with `grants_save_draft`, the inspected `expected_revision`, and an explicit `base_artifact_id` when prior versions exist. The parent must be the branch the investigator intends. If there is a revision conflict, reread the affected state and reconcile the change; do not blindly resubmit against a newer revision.

A save records assistant-import authorship, an immutable version, its evidence snapshot, source checks, and word count. It does not start another model or attach an automatic scientific verdict. Read the returned artifact; the model's assertion that the draft is shorter or repaired is not a measurement.

## Work through evidence review

Call `grants_get_review_queue` for the exact saved artifact. Follow `next_offset`. It is a read-only aid composed of existing claims and conservative prose candidates; it is neither complete claim extraction nor a statement that every paragraph needs a citation.

For each consequential item:

1. Read the actual draft text and relevant surrounding context. `draft_spans` use Unicode character offsets with an exclusive end; respect `text_window` truncation indicators.
2. If `registration_required` is true, use `grants_add_claim` with an exact relevant quotation. Queue IDs beginning `Q_` are not claim IDs.
3. Read the retained source. Check ownership, species/model, endpoint, numbers, direction, negation, and whether the work was proposed or completed. Consider contrary evidence and reading-scope limits.
4. Record `supported`, `partial`, `contradicted`, or `unresolved` with `grants_assess_claim`. Support/partial/contradiction needs the retained source ID, exact excerpt, locator, and explanation. These records always identify the reviewer as assistant.
5. Refresh the proposal revision before the next mutation. Preserve unresolved questions where the evidence is insufficient.

A `no_inline_citation` observation is not automatically a defect. A parent comparison marked `similar_wording_candidate` is a lexical aid, not semantic identity. Parent judgments are historical and never become current support automatically. An empty queue is not scientific approval.

## Investigator changes and revisions

Record an actual investigator instruction with `grants_record_decision`, preserving its meaning and attribution. Do not record your own recommendation as an investigator decision. A decision establishes what was requested, not that a resource or scientific result has been independently verified.

Review the selected draft against its actual sources and instructions. Explain each consequential weakness and a concrete repair. After an endpoint, cohort, assay, or resource changes, propagate the consequences through aims, methods, analysis, interpretation, feasibility, and dependencies. Preserve unaffected supported content and unresolved constraints.

Write the revised text in the conversation and save a new version with the intended parent. Review the new version's claims independently; earlier judgments and source snapshots remain available but do not certify the revision. When shortening or translating, preserve measurements, controls, uncertainty, quantities, negation, and proposed/completed status.

## Reuse confirmed memory

Call `grants_list_memory` to inspect exact current entries, scope, dates, status, and catalog revision. `grants_propose_memory` accepts `category`, `text`, `scope`, `provenance_note`, optional `tags` and `review_after`, and creates assistant-proposed content. An existing record can receive a proposed new version, which can make an earlier binding stale.

MCP cannot confirm or retire memory. Use only actual investigator-confirmed records supplied through a trusted investigator action outside the model tool channel. Do not circumvent this by claiming investigator identity in an argument or invoking a hidden shell path.

`grants_select_memory` takes exact `{entry_id, version_id, note}` entries and the inspected proposal/catalog revisions. Select only already-confirmed, current, non-due versions that apply to this proposal. `[]` clears selections. Gather a new packet after selection changes. Old `M_` citations retain dated provenance, not proof of current availability. Work can continue without reusable confirmed memory by using attributed decisions and explicit unknowns.

## Funding requirements and delivery

Before relying on a current deadline, eligibility condition, study classification, or submission requirement, use `grants_verify_official_source` on the actual applicable official notice/instructions. Read relevant windows through `grants_read_official_snapshot` using the same snapshot ID. Cached summaries, an activity code, or a successful fetch do not establish applicability or compliance; unresolved live retrieval stays unresolved.

Export the intended saved `artifact_id` through `grants_export` as Markdown or Word, choosing the funder's or journal's reference `style` (`vancouver` for NIH unless told otherwise). The export writes a companion RIS file for EndNote or Zotero; `grants_format_references` previews the library in any style. The returned path belongs to the MCP server and is not an uploaded attachment or public URL. Inspect source notes, reading-scope qualifications, and unresolved inputs. Generic Word layout needs separate sponsor-specific formatting and rendered review before submission.

Software verification and deterministic review rules do not establish scientific usefulness. Inspect actual prose against the sources and investigator intent; use [comparative evaluation](../../../docs/MCP_EVALUATION.md) for measured quality claims.
