"""Separate public NIH RePORTER library; never reads the personal grant index.

Official specifications consulted 2026-10-30:
https://api.reporter.nih.gov/
https://report.nih.gov/exporter-data-dictionary
https://report.nih.gov/faqs

Use the API for bounded discovery and ExPORTER CSV/ZIP files for bulk coverage.
Funded-project descriptions are descriptions of proposed/funded work, not
verified results, full applications, reviewer scores, or success predictors.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

API_URL = "https://api.reporter.nih.gov/v2/projects/search"
EXPORTER_URL = "https://reporter.nih.gov/exporter"
EVIDENCE_LABEL = "funded_project_description"
API_WINDOW = 15000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def initialize(db_path):
    """Create/open a distinct NIH SQLite database and return its connection.

    Reject databases with personal-library tables rather than mixing corpora.
    Caller owns the connection and must close it.
    """
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if existing & {"documents", "units"}:
        conn.close()
        raise ValueError("NIH data requires a separate database; personal-library tables were found.")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS nih_sources (
      source_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, origin TEXT NOT NULL,
      retrieved_at TEXT NOT NULL, sha256 TEXT, metadata_json TEXT NOT NULL,
      raw_json TEXT, status TEXT NOT NULL DEFAULT 'complete', row_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS nih_records (
      application_id TEXT PRIMARY KEY, fiscal_year INTEGER, core_project_number TEXT,
      project_number TEXT, subproject_id TEXT, title TEXT NOT NULL DEFAULT '',
      abstract TEXT NOT NULL DEFAULT '', public_health_relevance TEXT DEFAULT '',
      terms TEXT DEFAULT '', activity TEXT, ic TEXT, organization TEXT,
      principal_investigators TEXT, project_start TEXT, project_end TEXT,
      budget_start TEXT, budget_end TEXT, award_amount REAL,
      total_cost_subproject REAL, reporter_url TEXT,
      evidence_label TEXT NOT NULL DEFAULT 'funded_project_description',
      source_id TEXT REFERENCES nih_sources(source_id),
      abstract_source_id TEXT REFERENCES nih_sources(source_id), retrieved_at TEXT,
      raw_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS nih_core ON nih_records(core_project_number);
    CREATE INDEX IF NOT EXISTS nih_fy ON nih_records(fiscal_year);
    CREATE INDEX IF NOT EXISTS nih_activity ON nih_records(activity);
    CREATE TABLE IF NOT EXISTS nih_record_versions (
      application_id TEXT NOT NULL, source_id TEXT NOT NULL,
      record_json TEXT NOT NULL, PRIMARY KEY(application_id,source_id)
    );
    CREATE TABLE IF NOT EXISTS nih_abstracts (
      application_id TEXT PRIMARY KEY, abstract TEXT NOT NULL,
      source_id TEXT REFERENCES nih_sources(source_id), retrieved_at TEXT,
      raw_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS nih_api_runs (
      run_id TEXT PRIMARY KEY, query_key TEXT NOT NULL, criteria_json TEXT NOT NULL,
      next_offset INTEGER NOT NULL DEFAULT 0, received_count INTEGER NOT NULL DEFAULT 0,
      total_count INTEGER, search_id TEXT, requested_cap INTEGER NOT NULL,
      status TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      warnings_json TEXT NOT NULL DEFAULT '[]', error TEXT
    );
    CREATE INDEX IF NOT EXISTS nih_run_query ON nih_api_runs(query_key,updated_at);
    CREATE TABLE IF NOT EXISTS nih_api_run_records (
      run_id TEXT NOT NULL, application_id TEXT NOT NULL,
      PRIMARY KEY(run_id,application_id)
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS nih_fts USING fts5(
      title, abstract, public_health_relevance, terms,
      content='nih_records', content_rowid='rowid', tokenize='unicode61'
    );
    CREATE TRIGGER IF NOT EXISTS nih_ai AFTER INSERT ON nih_records BEGIN
      INSERT INTO nih_fts(rowid,title,abstract,public_health_relevance,terms)
      VALUES(new.rowid,new.title,new.abstract,new.public_health_relevance,new.terms);
    END;
    CREATE TRIGGER IF NOT EXISTS nih_ad AFTER DELETE ON nih_records BEGIN
      INSERT INTO nih_fts(nih_fts,rowid,title,abstract,public_health_relevance,terms)
      VALUES('delete',old.rowid,old.title,old.abstract,old.public_health_relevance,old.terms);
    END;
    CREATE TRIGGER IF NOT EXISTS nih_au AFTER UPDATE ON nih_records BEGIN
      INSERT INTO nih_fts(nih_fts,rowid,title,abstract,public_health_relevance,terms)
      VALUES('delete',old.rowid,old.title,old.abstract,old.public_health_relevance,old.terms);
      INSERT INTO nih_fts(rowid,title,abstract,public_health_relevance,terms)
      VALUES(new.rowid,new.title,new.abstract,new.public_health_relevance,new.terms);
    END;
    """)
    conn.commit()
    return conn


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return _json(value)
    return str(value).strip()


def _number(value, integer=False):
    if value is None or str(value).strip() == "":
        return None
    try:
        cleaned = str(value).replace(",", "").replace("$", "")
        return int(cleaned) if integer else float(cleaned)
    except (ValueError, TypeError):
        return None


def _source(conn, *, source_id, source_type, origin, metadata, raw=None, sha256=None, status="complete", row_count=0):
    conn.execute("""INSERT INTO nih_sources VALUES(?,?,?,?,?,?,?,?,?)
      ON CONFLICT(source_id) DO UPDATE SET status=excluded.status,
      row_count=excluded.row_count, metadata_json=excluded.metadata_json""",
      (source_id,source_type,str(origin),_now(),sha256,_json(metadata),
       _json(raw) if raw is not None else None,status,row_count))


def import_api_record(conn, record, source_id=None):
    """Idempotently import one V2 API record. Does not commit or access network.

    application_id identifies an annual application record; core_project_number
    groups related annual records. Never infer a missing core project number.
    Raw versions are retained when the same record arrives from another source.
    """
    app_id = _text(record.get("appl_id"))
    if not app_id or not re.fullmatch(r"\d+", app_id):
        raise ValueError("A numeric appl_id is required for an NIH project record.")
    if source_id is None:
        source_id = "record:" + _hash(_json(record))
        _source(conn, source_id=source_id, source_type="api_record", origin=API_URL,
                metadata={"application_id":app_id}, raw=record, row_count=1)
    elif not conn.execute("SELECT 1 FROM nih_sources WHERE source_id=?", (source_id,)).fetchone():
        raise ValueError("Unknown source_id; register the API page before importing its records.")
    organization = record.get("organization") or {}
    agency = record.get("agency_ic_admin") or {}
    vals = {
        "application_id": app_id, "fiscal_year": _number(record.get("fiscal_year"), True),
        "core_project_number": _text(record.get("core_project_num")),
        "project_number": _text(record.get("project_num")),
        "subproject_id": _text(record.get("subproject_id")),
        "title": _text(record.get("project_title")),
        "abstract": _text(record.get("abstract_text")),
        "public_health_relevance": _text(record.get("phr_text")),
        "terms": _text(record.get("pref_terms") or record.get("terms")),
        "activity": _text(record.get("activity_code")),
        "ic": _text(agency.get("abbreviation") or agency.get("code")) if isinstance(agency,dict) else _text(agency),
        "organization": _text(organization.get("org_name")) if isinstance(organization,dict) else _text(organization),
        "principal_investigators": _text(record.get("principal_investigators")),
        "project_start": _text(record.get("project_start_date")),
        "project_end": _text(record.get("project_end_date")),
        "budget_start": _text(record.get("budget_start")), "budget_end": _text(record.get("budget_end")),
        "award_amount": _number(record.get("award_amount")),
        "total_cost_subproject": _number(record.get("total_cost_sub_project")),
        "reporter_url": _text(record.get("project_detail_url")),
        "evidence_label": EVIDENCE_LABEL, "source_id":source_id,
        "abstract_source_id": source_id if record.get("abstract_text") else None,
        "retrieved_at": _now(), "raw_json": _json(record),
    }
    abstract = conn.execute("SELECT * FROM nih_abstracts WHERE application_id=?",(app_id,)).fetchone()
    if not vals["abstract"] and abstract:
        vals["abstract"] = abstract["abstract"]
        vals["abstract_source_id"] = abstract["source_id"]
    # Metadata-only bulk rows must not erase an abstract already obtained by API.
    # Raw snapshots preserve the original source, including absent/empty fields.
    updates = ",".join(f"{key}=COALESCE(NULLIF(excluded.{key},''),nih_records.{key})" for key in vals if key != "application_id")
    conn.execute(f"INSERT INTO nih_records({','.join(vals)}) VALUES({','.join('?' for _ in vals)}) ON CONFLICT(application_id) DO UPDATE SET {updates}",tuple(vals.values()))
    conn.execute("INSERT OR REPLACE INTO nih_record_versions VALUES(?,?,?)",(app_id,source_id,_json(record)))
    return app_id


def _pause(seconds, sleep):
    # Respect long Retry-After values while yielding in bounded sleep segments.
    while seconds > 0:
        part = min(30.0, seconds)
        sleep(part)
        seconds -= part


def _request_json(payload, *, sleep=time.sleep):
    body = _json(payload).encode("utf-8")
    for attempt in range(5):
        _pause(1.05, sleep)  # Every request, including retries, stays below 1/sec.
        request = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "Content-Type":"application/json", "Accept":"application/json",
            "User-Agent":"grantlib-local/1.0 (bounded NIH RePORTER retrieval)",
        })
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in {429,500,502,503,504} or attempt == 4:
                exc.close()
                raise
            retry = exc.headers.get("Retry-After", "")
            exc.close()
            try:
                delay = float(retry)
            except ValueError:
                try:
                    delay = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    delay = min(30, 2 ** (attempt+1))
            _pause(max(1,delay),sleep)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
            _pause(min(30,2 ** (attempt+1)),sleep)
    raise RuntimeError("NIH request retry loop exhausted")


def _run_summary(conn, run_id):
    row = dict(conn.execute("SELECT * FROM nih_api_runs WHERE run_id=?",(run_id,)).fetchone())
    row["criteria"] = json.loads(row.pop("criteria_json"))
    row["warnings"] = json.loads(row.pop("warnings_json"))
    row["coverage"] = "selected_query_only"
    row["unique_received_count"] = conn.execute("SELECT count(*) FROM nih_api_run_records WHERE run_id=?",(run_id,)).fetchone()[0]
    row["evidence_label"] = EVIDENCE_LABEL
    return row


def fetch_pilot(db_path, *, fiscal_years, public_topic, max_records, page_size=100,
                resume=True, request_json=None, search_fields='projecttitle,abstracttext,terms'):
    """Fetch at most max_records across a resumable public topic/year query.

    The cap applies cumulatively to the saved query, including earlier pages.
    Pass a larger max_records to continue it. No personal documents or library
    queries are read or uploaded. ``request_json`` is an offline-test hook taking
    one payload dict; production calls are rate-limited and retried internally.
    Returns a run summary with explicit status/coverage/warnings; failures retain
    completed pages and a checkpoint, then return status='failed'.
    """
    if not isinstance(max_records,int) or isinstance(max_records,bool) or not 1 <= max_records <= API_WINDOW:
        raise ValueError("max_records must be explicitly set to 1..15000; use ExPORTER for bulk coverage.")
    years = sorted(set(int(x) for x in fiscal_years))
    if not years or any(y < 1900 or y > datetime.now().year + 1 for y in years):
        raise ValueError("Specify valid fiscal_years for the bounded public query.")
    if not isinstance(public_topic,str) or not public_topic.strip() or len(public_topic) > 300:
        raise ValueError("Specify a short public_topic (1..300 characters), not private grant text.")
    if not isinstance(page_size,int) or not 1 <= page_size <= 500:
        raise ValueError("page_size must be 1..500.")
    if search_fields not in {'projecttitle,abstracttext,terms', 'projecttitle,abstracttext', 'projecttitle', 'abstracttext'}:
        raise ValueError('Use documented project title/abstract/term search fields.')
    criteria = {"fiscal_years":years,"advanced_text_search":{
        "operator":"and","search_field":search_fields,"search_text":public_topic.strip()}}
    query_key = _hash(_json(criteria))
    conn = initialize(db_path)
    requester = request_json or _request_json
    try:
        previous = conn.execute("SELECT * FROM nih_api_runs WHERE query_key=? ORDER BY updated_at DESC LIMIT 1",(query_key,)).fetchone() if resume else None
        if previous:
            run_id = previous["run_id"]
            if previous["status"] == "complete":
                return _run_summary(conn,run_id)
            offset, total, search_id = previous["next_offset"],previous["total_count"],previous["search_id"]
            warnings = json.loads(previous["warnings_json"])
            warnings = [w for w in warnings if w != "requested_cap_reached"]
        else:
            run_id = uuid.uuid4().hex
            offset,total,search_id,warnings = 0,None,None,[]
            conn.execute("INSERT INTO nih_api_runs(run_id,query_key,criteria_json,requested_cap,status,started_at,updated_at) VALUES(?,?,?,?,?,?,?)",(run_id,query_key,_json(criteria),max_records,"running",_now(),_now()))
        conn.execute("UPDATE nih_api_runs SET requested_cap=?,status='running',error=NULL,updated_at=? WHERE run_id=?",(max_records,_now(),run_id))
        conn.commit()
        status = "capped"
        while offset < max_records and offset < API_WINDOW:
            if total is not None and offset >= total:
                status = "complete"
                break
            limit = min(page_size,max_records-offset,API_WINDOW-offset)
            payload = {"offset":offset,"limit":limit,"sort_field":"appl_id","sort_order":"asc"}
            payload.update({"search_id":search_id} if search_id else {"criteria":criteria})
            try:
                response = requester(payload)
                if not isinstance(response,dict) or not isinstance(response.get("results"),list):
                    raise ValueError("NIH response lacks a results list")
                rows = response["results"]
                meta = response.get("meta") or {}
                current_total = _number(meta.get("total"),True)
                if current_total is not None:
                    if total is not None and current_total != total:
                        warnings.append("reported_total_changed_during_query")
                    total = current_total
                if total is not None and total > API_WINDOW:
                    warnings.append("query_exceeds_15000_record_window_use_partitions_or_exporter")
                search_id = meta.get("search_id") or search_id
                if not rows:
                    if total is not None and offset >= total:
                        status = "complete"
                    else:
                        status = "incomplete"
                        warnings.append("empty_page_before_confirmed_end")
                    break
                used = rows[:limit]
                if len(rows) > limit:
                    warnings.append("response_exceeded_requested_page_size")
                page_id = "api:" + _hash(_json({"run":run_id,"offset":offset,"response":response}))
                with conn:
                    _source(conn,source_id=page_id,source_type="api_page",origin=API_URL,
                            metadata={"run_id":run_id,"offset":offset,"payload":payload,"accepted_rows":len(used)},
                            raw=response,sha256=_hash(_json(response)),row_count=len(used))
                    added = 0
                    for record in used:
                        app_id = import_api_record(conn,record,page_id)
                        inserted = conn.execute("INSERT OR IGNORE INTO nih_api_run_records VALUES(?,?)",(run_id,app_id)).rowcount
                        added += inserted
                        if not inserted:
                            warnings.append("duplicate_application_ids_in_api_query")
                    offset += len(used)
                    conn.execute("UPDATE nih_api_runs SET next_offset=?,received_count=?,total_count=?,search_id=?,warnings_json=?,updated_at=? WHERE run_id=?",
                                 (offset,offset,total,search_id,_json(sorted(set(warnings))),_now(),run_id))
                if not added:
                    status = "incomplete"
                    warnings.append("page_contains_no_new_application_ids")
                    break
            except Exception as exc:
                conn.rollback()
                conn.execute("UPDATE nih_api_runs SET status='failed',error=?,updated_at=? WHERE run_id=?",(f"{type(exc).__name__}: {exc}",_now(),run_id))
                conn.commit()
                return _run_summary(conn,run_id)
        if total is not None and offset >= total:
            status = "complete"
        elif offset >= API_WINDOW:
            status = "window_limited"
            warnings.append("api_window_reached_use_partitions_or_exporter")
        elif offset >= max_records:
            warnings.append("requested_cap_reached")
        if total is None:
            warnings.append("source_total_unavailable")
        if status == "complete" and "duplicate_application_ids_in_api_query" in warnings:
            status = "incomplete"
        conn.execute("UPDATE nih_api_runs SET status=?,total_count=?,search_id=?,warnings_json=?,updated_at=? WHERE run_id=?",(status,total,search_id,_json(sorted(set(warnings))),_now(),run_id))
        conn.commit()
        return _run_summary(conn,run_id)
    finally:
        conn.close()


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda:stream.read(1024*1024),b""):
            digest.update(data)
    return digest.hexdigest()


def _csv_rows(path, encoding):
    """Stream CSV rows from files/ZIP members, without extracting ZIP paths."""
    csv.field_size_limit(16 * 1024 * 1024)
    path = Path(path)
    def rows(stream, member):
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"Missing CSV header: {path.name}:{member}")
        headers = {str(x).strip().upper() for x in reader.fieldnames}
        if "APPLICATION_ID" not in headers:
            raise ValueError(f"Missing APPLICATION_ID column: {path.name}:{member}")
        for index,row in enumerate(reader,2):
            if None in row:
                raise ValueError(f"Extra CSV fields at {path.name}:{member} row {index}")
            yield member,index,{str(k).strip().upper():v for k,v in row.items()}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir() and m.filename.lower().endswith(".csv")]
            if not members:
                raise ValueError(f"No CSV members in {path.name}")
            for member in members:
                with archive.open(member) as raw, io.TextIOWrapper(raw,encoding=encoding,newline="") as stream:
                    yield from rows(stream,member.filename)
    else:
        with path.open(encoding=encoding,newline="") as stream:
            yield from rows(stream,path.name)


def _bulk_record(row):
    return {
        "appl_id":row.get("APPLICATION_ID"),"fiscal_year":row.get("FY"),
        "core_project_num":row.get("CORE_PROJECT_NUM"),"project_num":row.get("FULL_PROJECT_NUM"),
        "subproject_id":row.get("SUBPROJECT_ID"),"project_title":row.get("PROJECT_TITLE"),
        "abstract_text":row.get("ABSTRACT_TEXT"),"phr_text":row.get("PHR"),
        "pref_terms":row.get("PROJECT_TERMS"),"activity_code":row.get("ACTIVITY"),
        "agency_ic_admin":{"code":row.get("ADMINISTERING_IC")},
        "organization":{"org_name":row.get("ORG_NAME")},
        "principal_investigators":row.get("PI_NAME(S)") or row.get("PI_NAMES"),
        "project_start_date":row.get("PROJECT_START"),"project_end_date":row.get("PROJECT_END"),
        "budget_start":row.get("BUDGET_START"),"budget_end":row.get("BUDGET_END"),
        "award_amount":row.get("TOTAL_COST"),"total_cost_sub_project":row.get("TOTAL_COST_SUB_PROJECT"),
        "project_detail_url":row.get("PROJECT_DETAIL_URL"),
        "exporter_row":row,
    }


def import_exporter(db_path, *, project_files=(), abstract_files=(), encoding="utf-8-sig"):
    """Stream explicitly supplied ExPORTER CSV/ZIP files into the public DB.

    Joins PROJECT/ABSTRACT records by APPLICATION_ID. Abstracts may be imported
    before or after projects; orphans are retained and reported. No downloads,
    archives extraction, full-coverage inference, or full-file memory loading.
    Returns per-file import results and archive statistics. Unsupported encodings
    fail transparently; pass the known encoding explicitly rather than replacing
    undecodable bytes. Completed identical files are skipped on repeated import.
    """
    if not project_files and not abstract_files:
        raise ValueError("Supply at least one project or abstract CSV/ZIP file.")
    conn = initialize(db_path)
    outcomes=[]
    try:
        for kind,files in (("abstract",abstract_files),("project",project_files)):
            for value in files:
                path=Path(value).expanduser().resolve()
                digest=_file_sha(path)
                source_id=f"exporter:{kind}:{digest}"
                old=conn.execute("SELECT status,row_count FROM nih_sources WHERE source_id=?",(source_id,)).fetchone()
                if old and old["status"]=="complete":
                    outcomes.append({"file":str(path),"kind":kind,"status":"already_imported","rows":old["row_count"],"sha256":digest})
                    continue
                metadata={"file":str(path),"encoding":encoding,"official_catalog":EXPORTER_URL,"join_key":"APPLICATION_ID"}
                _source(conn,source_id=source_id,source_type=f"exporter_{kind}",origin=path,
                        metadata=metadata,sha256=digest,status="running")
                conn.commit()
                count=0
                try:
                    for member,row_number,row in _csv_rows(path,encoding):
                        app_id=_text(row.get("APPLICATION_ID"))
                        if not app_id or not app_id.isdigit():
                            raise ValueError(f"Invalid APPLICATION_ID at {member} row {row_number}")
                        if kind=="abstract":
                            if "ABSTRACT_TEXT" not in row:
                                raise ValueError(f"Missing ABSTRACT_TEXT column in {member}")
                            abstract=_text(row.get("ABSTRACT_TEXT"))
                            raw={"csv_member":member,"csv_row":row_number,"fields":row}
                            conn.execute("INSERT INTO nih_abstracts VALUES(?,?,?,?,?) ON CONFLICT(application_id) DO UPDATE SET abstract=excluded.abstract,source_id=excluded.source_id,retrieved_at=excluded.retrieved_at,raw_json=excluded.raw_json",
                                         (app_id,abstract,source_id,_now(),_json(raw)))
                            conn.execute("UPDATE nih_records SET abstract=?,abstract_source_id=? WHERE application_id=?",(abstract,source_id,app_id))
                        else:
                            if "PROJECT_TITLE" not in row or "FY" not in row:
                                raise ValueError(f"Missing PROJECT_TITLE or FY column in {member}")
                            record=_bulk_record(row)
                            record["exporter_location"]={"csv_member":member,"csv_row":row_number}
                            import_api_record(conn,record,source_id)
                        count+=1
                        if count % 1000==0:
                            conn.execute("UPDATE nih_sources SET row_count=? WHERE source_id=?",(count,source_id))
                            conn.commit()
                    conn.execute("UPDATE nih_sources SET status='complete',row_count=? WHERE source_id=?",(count,source_id))
                    conn.commit()
                    outcomes.append({"file":str(path),"kind":kind,"status":"complete","rows":count,"sha256":digest})
                except Exception as exc:
                    conn.rollback()
                    metadata["error"]=f"{type(exc).__name__}: {exc}"
                    conn.execute("UPDATE nih_sources SET status='failed',metadata_json=? WHERE source_id=?",(_json(metadata),source_id))
                    conn.commit()
                    outcomes.append({"file":str(path),"kind":kind,"status":"failed","error":metadata["error"],"rows_committed":conn.execute("SELECT row_count FROM nih_sources WHERE source_id=?",(source_id,)).fetchone()[0]})
        return {"files":outcomes,"coverage":"supplied_files_only","stats":_stats(conn)}
    finally:
        conn.close()


def _collections(conn):
    """Expose every saved public query and its observed import coverage."""
    collections=[]
    for row in conn.execute("""SELECT r.*,
      (SELECT count(*) FROM nih_api_run_records m WHERE m.run_id=r.run_id) AS unique_received_count
      FROM nih_api_runs r ORDER BY r.updated_at DESC,r.run_id"""):
        criteria=json.loads(row["criteria_json"])
        text_search=criteria.get("advanced_text_search") or {}
        query=text_search.get("search_text") or "Public project query"
        collections.append({
            "run_id":row["run_id"], "query":query, "label":query,
            "years":criteria.get("fiscal_years",[]),
            "search_fields":text_search.get("search_field", ""),
            "received_count":row["received_count"],
            "unique_received_count":row["unique_received_count"],
            "total_count":row["total_count"], "status":row["status"],
            "updated_at":row["updated_at"],
        })
    return collections


def _stats(conn):
    count=conn.execute("SELECT count(*) FROM nih_records").fetchone()[0]
    return {
        "record_count":count,
        "core_project_count":conn.execute("SELECT count(DISTINCT core_project_number) FROM nih_records WHERE core_project_number<>''").fetchone()[0],
        "records_missing_core_project_number":conn.execute("SELECT count(*) FROM nih_records WHERE core_project_number IS NULL OR core_project_number=''").fetchone()[0],
        "records_with_abstract":conn.execute("SELECT count(*) FROM nih_records WHERE trim(abstract)<>''").fetchone()[0],
        "records_missing_abstract":conn.execute("SELECT count(*) FROM nih_records WHERE trim(abstract)=''").fetchone()[0],
        "orphan_abstracts":conn.execute("SELECT count(*) FROM nih_abstracts a LEFT JOIN nih_records r ON r.application_id=a.application_id WHERE r.application_id IS NULL").fetchone()[0],
        "fiscal_years":{str(r[0]):r[1] for r in conn.execute("SELECT fiscal_year,count(*) FROM nih_records GROUP BY fiscal_year ORDER BY fiscal_year")},
        "activities":{str(r[0]):r[1] for r in conn.execute("SELECT activity,count(*) FROM nih_records WHERE activity<>'' GROUP BY activity ORDER BY activity")},
        "sources":[dict(r) for r in conn.execute("SELECT source_type,status,count(*) AS count FROM nih_sources GROUP BY source_type,status")],
        "api_runs":[_run_summary(conn,r[0]) for r in conn.execute("SELECT run_id FROM nih_api_runs ORDER BY updated_at DESC LIMIT 20")],
        "collections":_collections(conn),
        "coverage":"imported_records_only_not_complete_nih_archive",
        "evidence_label":EVIDENCE_LABEL,
        "interpretation":"Annual application records are grouped by core project number; funded descriptions are not verified results or full applications.",
    }


def stats(db_path):
    conn=initialize(db_path)
    try:
        return _stats(conn)
    finally:
        conn.close()


def search(db_path, query, *, limit=20, fiscal_year=None, activity=None, collection=None, run_id=None):
    """Search only imported public records; return a source-labeled result dict.

    Exact lexical terms are ANDed. User text is quoted rather than interpreted
    as FTS operators. This is not semantic search or a novelty/funding score.
    """
    if not isinstance(limit,int) or not 1<=limit<=100:
        raise ValueError("limit must be 1..100")
    if len(query)>1000:
        raise ValueError("Query is too long")
    if collection and run_id and collection != run_id:
        raise ValueError("Supply one public collection ID")
    collection=collection or run_id
    conn=initialize(db_path)
    try:
        selected=None
        if collection:
            selected=next((item for item in _collections(conn) if item["run_id"]==collection),None)
            if selected is None:
                raise ValueError("Unknown public collection ID")
        tokens=re.findall(r"\w+",query,flags=re.UNICODE)
        fts=" AND ".join('"'+t.replace('"','""')+'"' for t in tokens)
        conditions,params=[],[]
        source="nih_records r"
        if tokens:
            source+=" JOIN nih_fts ON nih_fts.rowid=r.rowid"
            conditions.append("nih_fts MATCH ?")
            params.append(fts)
        if fiscal_year is not None:
            conditions.append("r.fiscal_year=?");params.append(int(fiscal_year))
        if activity:
            conditions.append("r.activity=?");params.append(activity)
        if collection:
            conditions.append("EXISTS (SELECT 1 FROM nih_api_run_records m WHERE m.application_id=r.application_id AND m.run_id=?)")
            params.append(collection)
        where=" WHERE "+" AND ".join(conditions) if conditions else ""
        total=conn.execute("SELECT count(*) FROM "+source+where,params).fetchone()[0]
        ordering="bm25(nih_fts),r.fiscal_year DESC" if tokens else "r.fiscal_year DESC,r.application_id"
        rows=conn.execute("SELECT r.application_id,r.fiscal_year,r.core_project_number,r.project_number,r.subproject_id,r.title,r.abstract,r.public_health_relevance,r.terms,r.activity,r.ic,r.organization,r.principal_investigators,r.project_start,r.project_end,r.award_amount,r.reporter_url,r.evidence_label,r.source_id,r.abstract_source_id,r.retrieved_at FROM "+source+where+" ORDER BY "+ordering+" LIMIT ?",[*params,limit]).fetchall()
        return {"query":query,"total":total,"results":[dict(r) for r in rows],"coverage":"imported_records_only","evidence_label":EVIDENCE_LABEL,"collection":selected}
    finally:
        conn.close()
