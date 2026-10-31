"""Offline, mechanism-aware retrieval of curated official grant guidance.

This catalog is separate from the private grant corpus. It contains bounded
paraphrases with official URLs and check dates, not a compliance determination.
The current funding opportunity and applicable policy notices remain controlling.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit


DEFAULT_GUIDANCE_PATH = Path(__file__).resolve().parent / "data" / "guidance_sources.json"
CONTENT_TYPES = {"advice", "requirement", "framework"}
OFFICIAL_AGENCY_DOMAINS = {"NIH": "nih.gov", "NSF": "nsf.gov", "HRSA": "hrsa.gov", "CDC": "cdc.gov"}
_ACTIVITY = re.compile(r"(?:[A-Z]\d{2}|[A-Z]{2}\d|[A-Z]\d[A-Z])(?:/(?:[A-Z]\d{2}|[A-Z]{2}\d|[A-Z]\d[A-Z]))?")
_STOPWORDS = {"a", "an", "and", "are", "as", "at", "be", "for", "from", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "the", "to", "we", "what", "with"}


def _tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    return set(re.findall(r"[a-z0-9]+", text)) - _STOPWORDS


def _validate(record: dict) -> None:
    for key in ("id", "title", "agency", "source_url", "checked_on", "content_type", "applies_to", "tips", "caveats", "tags"):
        if not record.get(key):
            raise ValueError(f"Guidance record is missing provenance/content field: {key}")
    if record["content_type"] not in CONTENT_TYPES:
        raise ValueError(f"Unknown guidance content type: {record['content_type']}")
    domain = OFFICIAL_AGENCY_DOMAINS.get(record["agency"])
    source = urlsplit(record["source_url"])
    host = (source.hostname or "").lower()
    if (not domain or source.scheme != "https" or source.username or source.password
            or not (host == domain or host.endswith("." + domain))):
        raise ValueError(f"Guidance source must be an official HTTPS {record['agency']} URL")
    try:
        checked = date.fromisoformat(record["checked_on"])
    except (ValueError, TypeError) as exc:
        raise ValueError("Guidance checked_on must be a valid ISO calendar date") from exc
    if checked > date.today():
        raise ValueError("Guidance checked_on cannot be in the future")
    if record.get('effective_from'):
        try:
            date.fromisoformat(record['effective_from'])
        except (ValueError, TypeError) as exc:
            raise ValueError('Guidance effective_from must be an ISO calendar date') from exc
    for key in ("applies_to", "not_applicable_to", "tips", "caveats", "tags", "source_sections"):
        values = record.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"Guidance {key} must be a list of nonempty strings")


def load_guidance(path: str | Path | None = None) -> list[dict]:
    """Load validated public guidance records; never performs a network request."""
    source = Path(path) if path is not None else DEFAULT_GUIDANCE_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Unsupported official guidance catalog schema")
    records = payload.get("sources")
    if not isinstance(records, list):
        raise ValueError("Guidance catalog must contain a sources list")
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Each guidance source must be an object")
        _validate(record)
        if record["id"] in seen:
            raise ValueError(f"Duplicate guidance source ID: {record['id']}")
        seen.add(record["id"])
    return records


def _normalize_mechanism(mechanism: str | None) -> str:
    value = (mechanism or "").upper().strip()
    if value.startswith("NIH "):
        value = value[4:]
    value = re.sub(r"\s+", "", value)
    return {"K": "K_SERIES", "KAWARDS": "K_SERIES", "NSF_ALL": "NSF", "NIH_ALL": "NIH"}.get(value, value)


def _scope_matches(scope: str, mechanism: str) -> bool:
    if scope == mechanism:
        return True
    if scope == "NIH_ALL":
        return bool((_ACTIVITY.fullmatch(mechanism) and mechanism[0] in "RUKFTPDGCS")
                    or mechanism in {"NIH", "K_SERIES"})
    if scope == "K_SERIES":
        return bool(re.fullmatch(r"K\d{2}(?:/R00)?", mechanism) or mechanism == "K_SERIES")
    if scope == "NSF_ALL":
        return mechanism == "NSF" or mechanism.startswith("NSF:")
    if mechanism == "K_SERIES":
        return bool(re.fullmatch(r"K\d{2}(?:/R00)?", scope))
    return False


def _applies(record: dict, mechanism: str) -> bool:
    if not mechanism or mechanism == "ALL":
        return True
    if mechanism in OFFICIAL_AGENCY_DOMAINS:
        return record["agency"] == mechanism
    if any(_scope_matches(scope, mechanism) for scope in record.get("not_applicable_to", [])):
        return False
    return any(_scope_matches(scope, mechanism) for scope in record["applies_to"])


def search_guidance(query: str = "", mechanism: str | None = None,
                    path: str | Path | None = None) -> list[dict]:
    """Search local paraphrases and filter by activity code or agency.

    K23 includes NIH-wide and K-series material, but not the research-project
    simplified-review framework. R01 includes that framework. NSF returns only
    NSF resources. An unknown mechanism returns no matches, not universal advice.
    An empty query browses all resources in the requested scope.
    """
    requested = _normalize_mechanism(mechanism)
    terms = _tokens(query)
    scored = []
    for record in load_guidance(path):
        if not _applies(record, requested):
            continue
        title = _tokens(record["title"])
        tags = _tokens(" ".join(record["tags"] + record["applies_to"]))
        body = _tokens(" ".join(record["tips"] + record["caveats"]))
        score = 4 * len(terms & title) + 3 * len(terms & tags) + len(terms & body)
        if terms and not score:
            continue
        scored.append((score, record))
    scored.sort(key=lambda item: (-item[0], item[1]["title"].casefold()))
    return [record for _, record in scored]
