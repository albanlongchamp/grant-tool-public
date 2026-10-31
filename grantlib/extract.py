"""Read grant documents without modifying sources or sending content off-device.

The returned units preserve source locators, not inferred facts. DOCX locators
refer to XML paragraph/table order; only PDFs have physical page numbers.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
NS = {"w": W[1:-1]}
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_XML_TOTAL = 128 * 1024 * 1024
MAX_ZIP_ENTRIES = 10000
COMMAND_TIMEOUT = 90
SUPPORTED_SUFFIXES = {".docx", ".pdf", ".doc", ".txt", ".md"}


def _empty(path: Path) -> dict:
    return {"units": [], "warnings": [], "metadata": {
        "format": path.suffix.lower().lstrip("."),
        "extractor": "grantlib.extract.v1", "status": "ok",
    }}


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    info = archive.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError(f"XML part exceeds {MAX_XML_BYTES} byte safety limit: {name}")
    value = archive.read(name)
    # Office documents do not require DTDs or entity declarations. Reject them
    # rather than exposing the parser to internal entity expansion.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", value, flags=re.I):
        raise ValueError(f"DTD or entity declarations are not accepted: {name}")
    return ET.fromstring(value)


def _visible_text(element: ET.Element) -> str:
    """Visible current text; omit deleted/moved-from text and field codes."""
    out: list[str] = []

    def visit(node: ET.Element) -> None:
        if node.tag in {W + "del", W + "moveFrom", W + "instrText", W + "delText"}:
            return
        if node.tag in {W + "t", M + "t"}:
            out.append(node.text or "")
            return
        if node.tag == MC + "AlternateContent":
            # Choice and fallback describe the same object. Reading both would
            # duplicate text (notably textboxes). Keep its primary XML view.
            branch = node.find(MC + "Choice")
            if branch is None:
                branch = node.find(MC + "Fallback")
            if branch is not None:
                visit(branch)
            return
        if node.tag == W + "tab":
            out.append("\t")
        elif node.tag in {W + "br", W + "cr"}:
            out.append("\n")
        elif node.tag == W + "noBreakHyphen":
            out.append("-")
        elif node.tag == W + "softHyphen":
            out.append("\u00ad")
        elif node.tag == W + "sym":
            out.append(f"[symbol font={node.get(W + 'font', '?')} char={node.get(W + 'char', '?')}]")
        elif node.tag in {W + "footnoteReference", W + "endnoteReference"}:
            label = "footnote" if node.tag == W + "footnoteReference" else "endnote"
            out.append(f" [{label} {node.get(W + 'id', '?')}] ")
        for child in node:
            visit(child)
        if node is not element and node.tag in {W + "p", W + "tr"}:
            out.append("\n")

    visit(element)
    # Whitespace at source boundaries is not information. Internal lines and
    # tabs are retained, including table/textbox paragraph boundaries.
    return "\n".join(line.rstrip() for line in "".join(out).splitlines()).strip()


def _docx(path: Path, result: dict) -> None:
    units = result["units"]
    warnings = result["warnings"]
    metadata = result["metadata"]
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError(f"Archive exceeds {MAX_ZIP_ENTRIES} entry safety limit")
        if len(names) != len(set(names)):
            raise ValueError("Archive has duplicate member names; source is ambiguous")
        xml_size = sum(x.file_size for x in infos if x.filename.lower().endswith(".xml"))
        if xml_size > MAX_XML_TOTAL:
            raise ValueError(f"XML parts exceed {MAX_XML_TOTAL} byte safety limit")
        metadata["embedded_media"] = [
            {"name": x.filename, "bytes": x.file_size}
            for x in infos if x.filename.startswith("word/media/") and not x.is_dir()
        ]
        if metadata["embedded_media"]:
            warnings.append("Embedded media inventoried only; image contents were not interpreted.")
        root = _xml(archive, "word/document.xml")
        body = root.find("w:body", NS)
        if body is None:
            raise ValueError("word/document.xml has no document body")

        styles: dict[str, str] = {}
        if "word/styles.xml" in names:
            try:
                for style in _xml(archive, "word/styles.xml").findall("w:style", NS):
                    name = style.find("w:name", NS)
                    if name is not None:
                        styles[style.get(W + "styleId", "")] = name.get(W + "val", "")
            except (ValueError, ET.ParseError, KeyError) as exc:
                warnings.append(f"Style names unavailable: {exc}")

        changes = {name: len(list(root.iter(W + name)))
                   for name in ("ins", "del", "moveFrom", "moveTo")}
        metadata["tracked_changes"] = changes
        if any(changes.values()):
            warnings.append("Tracked changes present: insertions and moved-to text included; deletions and moved-from text omitted. This is an extraction view, not an accepted revision.")
        if list(root.iter(W + "altChunk")):
            warnings.append("External/embedded altChunk content is not parsed; review the original.")
            metadata["status"] = "partial"
        if list(root.iter(W + "fldSimple")) or list(root.iter(W + "fldChar")):
            warnings.append("Word fields use their stored visible text; field codes were not executed or refreshed.")
        if list(root.iter(M + "oMath")):
            warnings.append("Equation text is retained in XML order; mathematical layout and operators require original-document inspection.")
        if list(root.iter(W + "sym")):
            warnings.append("Font-encoded symbols are retained as font/character placeholders; verify their glyphs in the original.")
        if list(root.iter(MC + "AlternateContent")):
            warnings.append("Alternate XML views were deduplicated by selecting the primary Choice, or its fallback when absent.")

        paragraph_numbers = {id(el): i for i, el in enumerate(body.iter(W + "p"), 1)}
        table_numbers = {id(el): i for i, el in enumerate(body.iter(W + "tbl"), 1)}
        metadata["paragraph_count"] = len(paragraph_numbers)
        metadata["table_count"] = len(table_numbers)
        comment_anchors: dict[str, list[str]] = {}
        section = ""

        def anchors(element: ET.Element, locator: str) -> None:
            for node in element.iter():
                if node.tag in {W + "commentRangeStart", W + "commentRangeEnd", W + "commentReference"}:
                    comment_id = node.get(W + "id", "?")
                    locations = comment_anchors.setdefault(comment_id, [])
                    if locator not in locations:
                        locations.append(locator)

        def heading(element: ET.Element) -> bool:
            style = element.find("w:pPr/w:pStyle", NS)
            sid = style.get(W + "val", "") if style is not None else ""
            sname = styles.get(sid, sid).lower().replace(" ", "")
            outline = element.find("w:pPr/w:outlineLvl", NS)
            return bool(re.match(r"heading\d*$", sname) or sname in {"title", "subtitle"}
                        or (outline is not None and outline.get(W + "val", "9") != "9"))

        def add(text: str, locator: str, kind: str, **extra) -> None:
            if text:
                units.append({"text": text, "locator": locator, "kind": kind,
                              "section": section, **extra})

        def walk(container: ET.Element) -> None:
            nonlocal section
            for element in container:
                if element.tag in {W + "del", W + "moveFrom"}:
                    continue
                if element.tag == W + "p":
                    text = _visible_text(element)
                    locator = f"paragraph {paragraph_numbers[id(element)]}"
                    anchors(element, locator)
                    is_heading = heading(element)
                    if is_heading and text:
                        section = text
                    add(text, locator, "heading" if is_heading else "paragraph")
                elif element.tag == W + "tbl":
                    table_no = table_numbers[id(element)]
                    # Rows may be inside content-control wrappers. Select those
                    # belonging to this table, not nested tables, in source order.
                    def rows(node):
                        for child in node:
                            if child.tag in {W + "del", W + "moveFrom", W + "tbl"}:
                                continue
                            if child.tag == W + "tr":
                                yield child
                            else:
                                yield from rows(child)
                    for row_no, row in enumerate(rows(element), 1):
                        locator = f"table {table_no} row {row_no}"
                        anchors(row, locator)
                        cells = [_visible_text(cell) for cell in row if cell.tag == W + "tc"]
                        if not cells:
                            cells = [_visible_text(row)]
                        text = " | ".join(cells).strip()
                        if any(cells):
                            add(text, locator, "table_row", cells=cells)
                elif element.tag == MC + "AlternateContent":
                    branch = element.find(MC + "Choice")
                    if branch is None:
                        branch = element.find(MC + "Fallback")
                    if branch is not None:
                        walk(branch)
                elif element.tag not in {W + "sectPr", W + "pPr", W + "tblPr"}:
                    walk(element)

        walk(body)

        def ancillary(part: str, label: str, child_name: str) -> None:
            if part not in names:
                return
            try:
                note_root = _xml(archive, part)
                for el in note_root.findall("w:" + child_name, NS):
                    if el.get(W + "type") in {"separator", "continuationSeparator", "continuationNotice"}:
                        continue
                    number = el.get(W + "id", "?")
                    text = _visible_text(el)
                    if not text:
                        continue
                    locator = f"{label} {number}"
                    extra = {"section": "", "source_part": part}
                    if label == "comment":
                        locations = comment_anchors.get(number, [])
                        extra.update({"anchors": locations, "author": el.get(W + "author", ""),
                                      "date": el.get(W + "date", "")})
                        if locations:
                            locator += " (anchor: " + "; ".join(locations) + ")"
                    units.append({"text": text, "locator": locator, "kind": label, **extra})
            except (ValueError, ET.ParseError, KeyError) as exc:
                warnings.append(f"Could not extract {part}: {exc}")
                metadata["status"] = "partial"

        ancillary("word/footnotes.xml", "footnote", "footnote")
        ancillary("word/endnotes.xml", "endnote", "endnote")
        ancillary("word/comments.xml", "comment", "comment")
        metadata["comment_count"] = sum(u["kind"] == "comment" for u in units)
        for part in names:
            if re.fullmatch(r"word/(?:header|footer)\d+\.xml", part):
                try:
                    text = _visible_text(_xml(archive, part))
                    if text:
                        units.append({"text": text, "locator": part, "kind": "header_footer", "section": ""})
                except (ValueError, ET.ParseError, KeyError) as exc:
                    warnings.append(f"Could not extract {part}: {exc}")
                    metadata["status"] = "partial"


def _blocks(text: str) -> list[str]:
    """Keep layout lines within a block; split only at blank lines."""
    return ["\n".join(line.rstrip() for line in block.splitlines()).strip()
            for block in re.split(r"\n[ \t]*\n+", text.replace("\r\n", "\n")) if block.strip()]


def _command(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=COMMAND_TIMEOUT,
                          check=False, encoding="utf-8", errors="replace")


def _has_xfa_placeholder(text: str) -> bool:
    """Recognize the specific Adobe viewer stub, not a generic 'please wait'."""
    normalized = " ".join(text.casefold().split())
    return (
        "your pdf viewer may not be able to display this type of document" in normalized
        and ("please wait" in normalized or "proper contents of the document" in normalized)
    )


def _is_xfa_boilerplate_block(text: str) -> bool:
    """Called only on a page containing the distinctive Adobe/XFA signature."""
    normalized = " ".join(text.casefold().split())
    return bool(
        re.fullmatch(r"please wait[.\u2026 ]*", normalized)
        or (_has_xfa_placeholder(text) and re.match(r"^(?:please wait|if this message)", normalized))
        or (normalized.startswith("you can upgrade to the latest version of adobe reader")
            and "adobe.com/go/reader_download" in normalized)
        or (normalized.startswith("for more assistance with adobe reader")
            and "adobe.com/go/acrreader" in normalized)
        or normalized.startswith("windows is either a registered trademark or a trademark of microsoft corporation")
        or normalized.startswith("mac is a trademark of apple inc.")
        or normalized.startswith("linux is the registered trademark of linus torvalds")
    )


def _pdf(path: Path, result: dict) -> None:
    executable = shutil.which("pdftotext")
    if not executable:
        raise RuntimeError("pdftotext is not installed; PDF text extraction unavailable")
    run = _command([executable, "-layout", "-enc", "UTF-8", str(path), "-"])
    if run.returncode:
        raise RuntimeError(f"pdftotext failed ({run.returncode}): {run.stderr.strip()}")
    if run.stderr.strip():
        result["warnings"].append("pdftotext reported: " + run.stderr.strip())
    pages = run.stdout.split("\f")
    if pages and not pages[-1].strip() and "\f" in run.stdout:
        pages.pop()
    result["metadata"]["page_count"] = len(pages)
    result["metadata"]["layout"] = "pdftotext -layout; no OCR or figure interpretation"
    low_text_pages = []
    xfa_pages = []
    omitted_placeholders = []
    for page_no, page in enumerate(pages, 1):
        has_placeholder = _has_xfa_placeholder(page)
        if has_placeholder:
            xfa_pages.append(page_no)
        retained_text = []
        for block_no, block in enumerate(_blocks(page), 1):
            if has_placeholder and _is_xfa_boilerplate_block(block):
                omitted_placeholders.append(f"page {page_no}, block {block_no}")
                continue
            retained_text.append(block)
            result["units"].append({"text": block, "locator": f"page {page_no}, block {block_no}",
                                    "kind": "paragraph", "section": "", "page": page_no})
        if len(re.sub(r"\W", "", " ".join(retained_text))) < 40 and not has_placeholder:
            low_text_pages.append(page_no)
    if xfa_pages:
        result["metadata"]["xfa_placeholder_pages"] = xfa_pages
        result["metadata"]["omitted_viewer_placeholder_blocks"] = omitted_placeholders
        result["metadata"]["status"] = "partial"
        result["warnings"].append(
            "Adobe/XFA viewer placeholder detected on pages " + ", ".join(map(str, xfa_pages))
            + ". Placeholder boilerplate was excluded from searchable content; the form's substantive content is not exposed by this extraction. Obtain a readable export or inspect the original with an XFA-capable viewer."
        )
    if low_text_pages:
        result["warnings"].append("Pages with little or no extractable text (blank, figure, or scan; review original/OCR if needed): "
                                  + ", ".join(map(str, low_text_pages)))
        result["metadata"]["low_text_pages"] = low_text_pages
        result["metadata"]["status"] = "partial"
    result["warnings"].append("PDF text only: images, equations, tables, columns, and reading order require source inspection.")


def _plain(text: str, result: dict, kind: str = "paragraph") -> None:
    for index, block in enumerate(_blocks(text), 1):
        result["units"].append({"text": block, "locator": f"text block {index}", "kind": kind, "section": ""})


def extract_file(path: Path) -> dict:
    """Extract complete available text with locators and explicit limitations.

    Failures return metadata.status='error' plus an explanatory warning. There
    is no silent text truncation. Safety limits reject oversized XML parts.
    Unsupported formats remain inventory-only with status='unsupported'.
    """
    path = Path(path)
    result = _empty(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            _docx(path, result)
        elif suffix == ".pdf":
            _pdf(path, result)
        elif suffix in {".doc", ".rtf"}:
            executable = shutil.which("textutil")
            if not executable:
                raise RuntimeError(f"textutil is unavailable; {suffix} requires conversion")
            run = _command([executable, "-convert", "txt", "-stdout", str(path)])
            if run.returncode:
                raise RuntimeError(f"textutil failed ({run.returncode}): {run.stderr.strip()}")
            if run.stderr.strip():
                result["warnings"].append("textutil reported: " + run.stderr.strip())
            _plain(run.stdout, result)
            format_name = "Legacy DOC" if suffix == ".doc" else "RTF"
            result["warnings"].append(f"{format_name} converted to text in memory; comments, tracked changes, fields, tables, and images may be incomplete. Inspect original.")
            result["metadata"]["status"] = "partial"
        elif suffix in {".txt", ".md"}:
            # Strict decoding ensures damaged/unknown encoding is reported.
            _plain(path.read_text(encoding="utf-8-sig"), result)
        else:
            result["metadata"]["status"] = "unsupported"
            result["warnings"].append(f"Inventory only: {suffix or 'extensionless'} format is not supported for text extraction.")
    except (OSError, ValueError, RuntimeError, UnicodeError, ET.ParseError,
            zipfile.BadZipFile, KeyError, subprocess.TimeoutExpired) as exc:
        result["metadata"]["status"] = "error"
        result["warnings"].append(f"Extraction failed: {type(exc).__name__}: {exc}")
    if not result["units"] and result["metadata"]["status"] in {"ok", "partial"}:
        result["metadata"]["status"] = "no_text"
        if result["metadata"].get("xfa_placeholder_pages"):
            result["warnings"].append("No substantive text extracted from the Adobe/XFA form; a readable export or manual review is required.")
        else:
            result["warnings"].append("No extractable text found; the source needs manual review or OCR.")
    result["metadata"]["unit_count"] = len(result["units"])
    result["metadata"]["character_count"] = sum(len(u["text"]) for u in result["units"])
    return result
