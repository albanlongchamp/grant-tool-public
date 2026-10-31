"""Conservative, transparent annotations for a local grant archive.

These rules describe language in a document; they do not verify scientific
claims, authorship, ownership, submission status, or current lab capability.
Source text is never rewritten. Keep these annotations separate from it.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath


ANNOTATION_METHOD = "rules_v1"


def _norm(value: str) -> str:
    """Normalize for matching only, retaining original identifiers elsewhere."""
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[‐‑‒–—−]", "-", value)


def _has(pattern: str, value: str) -> bool:
    return re.search(pattern, value, re.IGNORECASE) is not None


def classify_document(relative_path: str) -> dict:
    """Return candidate document identity inferred only from its path.

    Explicit collaborator/template folders and filename prefixes retain that
    role even when nested in a personal proposal or resubmission folder.
    These neutral naming conventions are hints, not ownership verification;
    configure a source's default role or curate documents when needed.
    """
    path = _norm(relative_path.replace("\\", "/"))
    parts = PurePosixPath(path).parts
    filename = parts[-1] if parts else path
    folders = set(parts[:-1])
    flags = ["document_identity_inferred_from_path"]
    collaborator_folders = {
        "collaborator", "collaborators", "template", "templates", "sample_grants",
    }
    collaborator_filename = _has(
        r"^(?:collaborator|template|sample)[. _-]", filename
    )
    if folders & {"literature", "papers"}:
        role = "literature"
    elif folders & collaborator_folders or collaborator_filename:
        role = "collaborator_or_template"
    elif "unreviewed_archive" in folders:
        role = "ownership_uncertain"
        flags.append("ownership_uncertain")
    else:
        role = "own_grant"

    if folders & {"resubmission", "revision"}:
        cycle = "resubmission"
    elif folders & {"historical", "previous_submission", "unreviewed_archive"}:
        cycle = "historical"
    else:
        cycle = "unspecified"
    if cycle != "unspecified":
        flags.append("cycle_inferred_from_folder")

    if role == "literature":
        kind = "literature"
    elif _has(r"summary[ _-]*statement|remark|comment", filename):
        kind = "reviewer_feedback"
    elif _has(r"rebut[at]?al|rebuttal|introduction", filename):
        kind = "reviewer_response"
    elif _has(r"(?:assist|application|draft)[ _-]*(?:final[ _-]*)?preview|full[ _-]*proposal", filename):
        kind = "full_application"
    elif _has(r"research[ _-]*(?:strategy|plan|s[_ .-])|proposal", filename):
        kind = "research_strategy"
    elif _has(r"specific[ _-]*aim|(?:^|[_ -])sa(?:[_ .-]|$)", filename):
        kind = "specific_aims"
    elif _has(r"(?:^|[_ -])\d?[krf]\d{2}[a-z]{2}\d{6}-\d{2}", filename):
        kind = "nih_grant_record"
        flags.append("application_or_summary_statement_requires_review")
    elif _has(r"project[ _-]*summary|projectsummary", filename):
        kind = "project_summary"
    elif _has(r"project[ _-]*narrative|projectnarrative", filename):
        kind = "project_narrative"
    elif _has(r"reference", filename):
        kind = "references"
    elif _has(r"biosketch|common[ _-]*form[ _-]*bio|(?:^|[_ -])bio(?:[_ .-]|$)", filename):
        kind = "biosketch"
    elif _has(r"career|candidate|training|mentor|rcr", filename):
        kind = "career_development"
    elif _has(r"statistical|power|study[ _-]*design", filename):
        kind = "analysis_plan"
    elif _has(r"protocol", filename):
        kind = "protocol"
    elif _has(r"facilit|equipment|institutional|environment|resource|authenti", filename):
        kind = "resources"
    elif _has(r"human[ _-]*subject|inclusion|recruit|data[ _-]*safety|dissemination|sharing|dms|biohazard", filename):
        kind = "research_administration"
    elif _has(r"budget|checklist|cover|timeline|milestone", filename):
        kind = "administration"
    elif _has(r"letter|(?:^|[_ -])lo[rs](?:[_ .-]|$)", filename) or "letters" in folders:
        kind = "support_letter"
    elif PurePosixPath(filename).suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".ai", ".psd", ".prism"}:
        kind = "figure_or_data_asset"
    elif _has(r"figure|fig\d|flowchart", filename):
        kind = "figure_or_data_asset"
    else:
        kind = "unclassified"
    return {"role": role, "cycle": cycle, "kind": kind, "review_flags": flags}


# Research topics are archive-specific. None are built in; configure them under
# "vocabulary" -> "topics" as {tag: regular expression}. See config.example.json.
TOPIC_RULES: dict[str, str] = {}

TECHNIQUE_RULES = {
    "spatial_transcriptomics": r"spatial[ -](?:transcriptom\w*|gene[ -]expression)|\bvisium\b",
    "single_cell_rna_seq": r"single[ -]cell[ -](?:rna|transcriptom)|\bscrna[ -]?(?:seq)?\b|\bsnrna[ -]?(?:seq)?\b|single[ -]nucleus[ -]rna",
    "bulk_rna_seq": r"bulk[ -]rna|\brna[ -]seq(?:uencing)?\b",
    "metabolomics": r"\bmetabolom\w*|\blc[ -]ms\b|liquid[ -]chromatography.{0,20}mass[ -]spectrom",
    "mass_spectrometry": r"mass[ -]spectrom\w*|\blc[ -]ms\b|\bgc[ -]ms\b",
    "flow_cytometry": r"flow[ -]cytometr\w*|\bfacs\b",
    "mass_cytometry": r"mass[ -]cytometr\w*|\bcytof\b",
    "histology": r"histolog\w*|histopatholog\w*|\bh&e\b",
    "electron_microscopy": r"electron[ -]microscop\w*|\btem\b",
    "qpcr": r"\b(?:rt[ -]?)?qpcr\b|quantitative[ -](?:real[ -]time[ -])?pcr",
    "western_blot": r"western[ -]blot|immunoblot",
    "immunostaining": r"immunohistochem\w*|immunofluorescen\w*|immunostain\w*",
    "genetic_manipulation": r"cre[ -]lox|knock[ -]?out|knock[ -]?in|\bcrispr\b|gene[ -]edit\w*|overexpress\w*",
    "high_throughput_screening": r"high[ -]throughput[ -](?:in[ -]vitro[ -])?screen\w*|compound[ -]librar\w*",
    "randomized_trial": r"randomi[sz]ed.{0,25}(?:trial|controlled)|\brct\b|randomi[sz]ation|randomly[ -]assigned",
    "differential_expression": r"differential[ -](?:gene[ -])?expression|\bedger\b|\bdeseq2?\b",
    "microbiome_profiling": r"microbiom\w*|metagenom\w*|16s[ -](?:rrna|sequenc)",
}

MODEL_RULES = {
    "mouse": r"\bmice\b|\bmouse\b|\bmurine\b|\bc57bl",
    "rat": r"\brats?\b",
    "human": r"\bhumans?\b|\bpatients?\b|\bparticipants?\b",
    "cell_culture": r"in[ -]vitro|cell[ -]cultur\w*|cell[ -]lines?",
    "ex_vivo": r"ex[ -]vivo",
    "clinical_cohort": r"\bcohort\b|patients[ -](?:undergoing|enrolled)|participants[ -](?:undergoing|enrolled)",
}

CATEGORY_RULES = {
    "background": r"\bbackground\b|state[ -]of[ -]the[ -]art|prior[ -]work|previous[ -]studies",
    "significance": r"\bsignificance\b|\bimpact\b|unmet[ -]need|statement[ -]of[ -]need",
    "innovation": r"\binnovation\b|\binnovative\b|novel[ -](?:approach|strategy|method)",
    "hypothesis": r"hypothesi[sz]\w*|\bhypothes[ie]s\b",
    "aims": r"(?:specific[ -])?\baims?\s*[123](?:[abc])?\b|specific[ -]aims|study[ -]objectives",
    "preliminary_results": r"preliminary[ -](?:data|result|finding|stud)|our[ -](?:data|results)[ -](?:show|suggest|indicat)|we[ -](?:have[ -])?(?:previously[ -])?(?:demonstrated|showed|shown|found|observed)",
    "methods": r"\bmethods?\b|\bapproach(?:es)?\b|experimental[ -]design|study[ -]design|\bprotocol\b|will[ -](?:measure|assess|perform|analy[sz]e|use|test|collect|quantify)",
    "analysis": r"statistical[ -](?:design|analysis|analyses|power)|sample[ -]size|power[ -](?:analysis|calculation)|primary[ -]endpoint|differential[ -](?:gene[ -])?expression",
    "pitfalls": r"\bpitfalls?\b|\blimitations?\b|alternative[ -](?:approach|strateg|method)|potential[ -](?:problem|challeng)|contingenc\w*",
    "references": r"^(?:\d+[. ]*)?(?:references|bibliography|literature[ -]cited)\s*:?$",
    "reviewer_feedback": r"reviewer|summary[ -]statement|major[ -](?:concern|weakness)|\bweaknesses\b|\bstrengths\b|critique",
    "resources": r"\bfacilities\b|\bequipment\b|institutional[ -]environment|available[ -]resources|research[ -]resources",
    "career_development": r"career[ -](?:development|goal)|training[ -]plan|mentorship|mentoring[ -]plan|responsible[ -]conduct",
}

_PROPOSED = re.compile(
    r"\b(?:we|i|our team|investigators|this study|the study|the project)\s+(?:will|plan to|propose to|aim to|intend to)|"
    r"\bwill\s+(?:be\s+)?(?:test|measur|assess|analy[sz]|evaluat|perform|collect|investigat|determin|use|conduct|recruit|enroll|randomi[sz]|quantif|examin|undergo|compar|screen|sacrific|treat|obtain|explor|study|include|focus)|"
    r"\b(?:we\s+)?(?:hypothesi[sz]e|propose|predict|anticipate|expect)\b|"
    r"\b(?:anticipated|expected)\s+(?:results|outcomes)|\b(?:proposed|planned)\s+(?:study|experiment|work|research|approach|trial)|"
    r"\b(?:our|the|central|working)\s+(?:central\s+|working\s+)?hypothesis\s+is\b|"
    r"\b(?:aim|objective)\s+(?:\d[a-c]?\s*[:.]?\s*)?(?:is\s+)?to\b",
    re.IGNORECASE,
)
_REPORTED = re.compile(
    r"\b(?:we|i|our team)\s+(?:(?:have|had|previously|recently|already)\s+)*(?:demonstrated|showed|shown|found|observed|established|reported|identified|measured|performed)\b|"
    r"\bour\s+(?:preliminary\s+)?(?:data|results|findings)\s+(?:show|suggest|indicat|demonstrat|reveal)|"
    r"\bpreliminary\s+(?:data|results|findings|analysis)\s+(?:show|suggest|indicat|demonstrat|reveal)|"
    r"\b(?:has|have)\s+been\s+(?:shown|demonstrated|reported|observed)\b|"
    r"\b(?:results|data|findings)\s+(?:showed|demonstrated|revealed|indicated)\b",
    re.IGNORECASE,
)
_NEGATED_REPORT = re.compile(
    r"\b(?:have|has|had|did|do|does)\s+(?:not|never)\s+(?:yet\s+)?(?:demonstrat\w*|show\w*|find|found|observ\w*)",
    re.IGNORECASE,
)


def annotate(text: str, section: str = "", document: dict | None = None,
             topic_rules: dict | None = None) -> dict:
    """Annotate one source passage, preserving uncertainty and provenance.

    Tags are heuristic retrieval aids, not atomic claim extraction. A passage
    with both future experiments and reported results remains mixed; readers
    must inspect the actual passage. Published literature is also unverified.
    """
    document = document or {}
    normalized = _norm(text)
    section_norm = _norm(section)
    context = section_norm + "\n" + normalized
    categories = {tag for tag, pattern in CATEGORY_RULES.items() if _has(pattern, context)}
    kind = document.get("kind", "")
    kind_categories = {
        "references": "references", "reviewer_feedback": "reviewer_feedback",
        "reviewer_response": "reviewer_feedback", "protocol": "methods",
        "analysis_plan": "analysis", "resources": "resources",
        "career_development": "career_development",
    }
    if kind in kind_categories:
        categories.add(kind_categories[kind])
    if _has(r"^(?:\d+[. ]*)?(?:references|bibliography|literature[ -]cited)\b", section_norm):
        categories.add("references")
    if not categories:
        categories.add("unclassified")

    topics = sorted(tag for tag, pattern in {**TOPIC_RULES, **(topic_rules or {})}.items() if _has(pattern, context))
    techniques = sorted(tag for tag, pattern in TECHNIQUE_RULES.items() if _has(pattern, context))
    models = sorted(tag for tag, pattern in MODEL_RULES.items() if _has(pattern, context))
    # Do not treat the "human" substring of nonhuman as a human model.
    if "single_cell_rna_seq" in techniques and not _has(r"bulk[ -]rna", context):
        techniques = [x for x in techniques if x != "bulk_rna_seq"]

    flags = []
    proposed = bool(_PROPOSED.search(normalized))
    reported = bool(_REPORTED.search(normalized))
    if _NEGATED_REPORT.search(normalized):
        flags.append("negated_evidence_language")
    if "references" in categories and not (proposed or reported):
        evidence_status = "not_applicable"
        flags.append("bibliography_is_not_verified_claim_support")
    elif kind in {"administration", "biosketch", "support_letter", "research_administration", "figure_or_data_asset"}:
        evidence_status = "not_applicable"
    elif "reviewer_feedback" in categories:
        evidence_status = "not_applicable"
        flags.append("reviewer_or_response_text_not_primary_evidence")
    elif proposed and reported:
        evidence_status = "mixed_or_unclear"
        flags.append("mixed_proposal_and_reported_evidence")
    elif proposed:
        evidence_status = "proposed"
    elif reported:
        evidence_status = "reported_result_unverified"
        flags.append("reported_claim_requires_source_verification")
    else:
        evidence_status = "mixed_or_unclear"
        flags.append("evidence_status_requires_review")
    if document.get("role") == "collaborator_or_template":
        flags.append("do_not_attribute_collaborator_work_to_user")
    if document.get("role") == "ownership_uncertain":
        flags.append("ownership_uncertain")
    if document.get("role") == "literature":
        flags.append("literature_support_not_verified")
    if "preliminary_results" in categories:
        flags.append("preliminary_claim_not_independently_verified")
    if kind == "resources" or "resources" in categories:
        flags.append("historical_capability_requires_current_confirmation")
    return {
        "categories": sorted(categories), "topics": topics, "techniques": techniques,
        "models": models, "evidence_status": evidence_status,
        "review_flags": sorted(set(flags)), "annotation_method": ANNOTATION_METHOD,
    }


# Small, curated synonym groups for lexical retrieval of common method names.
# Archive-specific synonyms belong in "vocabulary" -> "synonyms" in the config.
SYNONYM_GROUPS = (
    ("spatial transcriptomics", "spatial gene expression", "visium"),
    ("rna seq", "rna-seq", "rna sequencing"),
    ("single cell rna sequencing", "single-cell rna sequencing", "scrna-seq", "scrnaseq"),
    ("cytof", "mass cytometry"),
    ("rct", "randomized controlled trial", "randomised controlled trial"),
)


def expand_query(query: str, extra_groups=()) -> list[str]:
    """Return the original normalized query plus matching synonym phrases.

    Values are plain lexical text, never SQL or FTS syntax. The caller must
    quote them when building an FTS query. Expansion is bounded and does not
    cross-expand synonyms transitively.
    """
    normalized = re.sub(r"\s+", " ", _norm(query)).strip()
    if not normalized:
        return []
    result = [normalized]
    for group in (*SYNONYM_GROUPS, *extra_groups):
        if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", normalized) for term in group):
            result.extend(term for term in group if term not in result)
    return result


def parse_references(text: str) -> dict[str, list[str]]:
    """Extract explicit DOI/PMID strings without resolving or validating them.

    Bibliographic citation numbers (e.g. superscript 12) are not treated as
    PubMed IDs. Original spelling and case of identifiers are retained.
    """
    dois = []
    for match in re.finditer(r"\b10\.\d{4,9}/[^\s<>\"']+", text, re.IGNORECASE):
        doi = match.group().rstrip(".,;:")
        while doi.endswith(")") and doi.count(")") > doi.count("("):
            doi = doi[:-1]
        while doi.endswith("]") and doi.count("]") > doi.count("["):
            doi = doi[:-1]
        if doi not in dois:
            dois.append(doi)
    pmids = []
    for pattern in (r"\bPMID\s*:?\s*(\d{6,10})\b", r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{6,10})\b"):
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if match.group(1) not in pmids:
                pmids.append(match.group(1))
    return {"dois": dois, "pmids": pmids}
