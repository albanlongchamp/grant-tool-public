# Refreshing the shared grant memory

Choose one canonical host for the private databases and point all MCP clients to it. Git distributes software; it does not synchronize drafts or databases. Refresh jobs run on the host where the original grant folders are mounted. No model calls occur during maintenance.

Configure `refresh` in the private `config.local.json`, using `config.example.json` as a starting point. Enable it only on the chosen host. Public-topic queries are explicit configuration: no private aims, archive paragraphs or proposal briefs are sent to public APIs. The public collections remain separate from personal evidence and saved proposal history.

```sh
.venv/bin/python -m grantlib refresh
.venv/bin/python -m grantlib refresh-watch --force
.venv/bin/python -m grantlib refresh-status
.venv/bin/python -m grantlib refresh-install --load
```

Use `--config /absolute/path/config.local.json` before the command when outside the repository. Installation creates two per-user launch agents. The public job checks daily at 03:15 in the host's time zone, refreshing successful collections weekly and retrying failed/partial collections. The archive job scans every five minutes. A changed inventory must remain stable for at least 60 seconds (normally two ticks, about 5–10 minutes) before rebuilding. Daily content hashes catch changes missed by file metadata. Both run after loading, so startup can catch up. The user session, host and source drives must be available; these jobs do not wake a powered-off Mac.

On macOS, allow the specific prompt asking whether Python may access files managed by Google Drive. A background launch agent has a different privacy identity from an interactive SSH session; successful manual indexing does not establish that this permission was granted. The scheduled supervisor bounds archive jobs to ten minutes and public jobs to one hour, stopping its own child on timeout and recording a failed attempt without advancing the successful date. It also cleans up its child when unloaded. This prevents a stalled permission prompt from indefinitely holding the shared maintenance lock; the prior usable indexes and saved drafts remain intact. Initial cloud downloads can take longer, so complete the first manual index with the drives available before relying on the periodic watcher. Check the specific timeout error and the host's privacy prompt when a scheduled scan remains stale.

Public refresh waits for an active archive job rather than skipping its daily check; that wait counts toward the supervisor's one-hour limit. Archive ticks remain nonblocking while public maintenance runs.

`grants_status` and discovery tools show `last_attempt`, `last_success`, `status`, coverage/warnings and `stale` per layer. `last_success` means that the configured bounded collection completed; it does not mean the whole public literature is indexed or scientific claims are verified. Partial or failed runs retain the previous successful date. A crashed worker remains visibly `running`/stale until a retry; OS locks release when the process exits. An archive's `last_success` is its last successful scan; `index_built_at` is the last rebuild. Settings changes bypass the weekly skip.

| Collection | Update policy | Scope and limits |
| --- | --- | --- |
| Personal archive | Stable changes trigger an atomic rebuild with content-addressed extraction caches. Manual ingestion and curation share a writer lock. | Originals are never changed. New extraction errors, missing/empty drives or changes during a build retain the prior index. Native Google Docs require updated local exports; cloud-only edits are not observed automatically. |
| PubMed | Overlapping creation/revision date windows, per-query checkpoints, bounded oldest-first PMID rechecks; version only new/changed article content. | Metadata and abstracts only. Initial history is `initial_days`; increasing it triggers backfill. Capped/failed query windows do not advance. Known-ID absence is flagged, not interpreted as retraction. |
| NIH RePORTER | Additions with date overlap; full topic reconciliation every 28 days; rotating known-ID rechecks each run. Import new/changed payloads only. | No documented modified-record cursor: comparison requests necessarily redownload some unchanged records. `rolling_years` tracks federal fiscal years, including the October rollover; alternatively set explicit `fiscal_years`. Old descriptions remain preserved. |
| Official guidance | Conditional ETag/Last-Modified requests where supported; immutable raw-page snapshots and hashes. | Downloading a changed page does not revise or reapprove the curated summary. Per-page status distinguishes failure, changed content and unsupported text formats. |

PubMed's creation/revision fields and API limits are documented in [PubMed Help](https://pubmed.ncbi.nlm.nih.gov/help/#modification-date-lr) and [E-utilities](https://www.ncbi.nlm.nih.gov/books/NBK25499/). NIH's supported criteria are in the [official RePORTER API](https://api.reporter.nih.gov/). Neither source is evidence of a study's correctness or a grant's writing quality.

Before using a deadline or submission requirement, the calling agent must fetch and read the applicable official page live through `grants_verify_official_source`. That tool retains a timestamped, hashed page version and provides a snapshot ID for further reading. It never asserts that a requirement has been satisfied. Failed live verification must remain unresolved; a cached summary is not a substitute for the current opportunity. HTTPS official-domain and redirect restrictions, timeouts and response limits apply.

Refreshes never update `studio.sqlite3`: saved drafts, their evidence packets, resolved reference snapshots and investigator decisions keep the original source versions. Newly discovered material is not automatically admitted as proposal evidence. Explicit evidence gathering/retention is still required for a new draft version.

Maintenance state lives in `.local/refresh/state.json`; adapter histories are in the separate discovery databases. Logs are `.local/refresh/logs/{public,archive}.{out,err}.log`. Inspect them and `refresh-status` if a layer is stale. Raise a per-topic cap or narrow an explicit public scope if coverage is partial; do not mark a capped response complete. Rerun one public layer with `refresh --layers nih`, `publications` or `guidance`. `refresh-watch --force` bypasses debounce, but still protects the old index on unstable inputs/new extraction failures. If a source was intentionally removed, reconfigure the source list and run manual ingestion before resuming the watcher.

To stop the schedules without deleting data:

```sh
launchctl bootout "gui/$(id -u)/org.grantlib.refresh.public"
launchctl bootout "gui/$(id -u)/org.grantlib.refresh.archive"
```

Remove the corresponding plists from `~/Library/LaunchAgents/` only if permanent removal is intended. Reinstalling with `refresh-install --load` updates and reloads the same labels.
