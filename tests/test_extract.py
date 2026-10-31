"""Checks for source preservation and extraction failure reporting."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from grantlib.extract import extract_file


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def docx(self, body, **parts):
        path = self.root / "fixture.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
            for name, text in parts.items():
                archive.writestr(name, text)
        return path

    def test_order_tables_and_headings(self):
        path = self.docx('''<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Methods</w:t></w:r></w:p>
        <w:p><w:r><w:t>Before table.</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Control</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>n=10</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><w:r><w:t>After table.</w:t></w:r></w:p>''')
        result = extract_file(path)
        self.assertEqual([u["text"] for u in result["units"]], ["Methods", "Before table.", "Control | n=10", "After table."])
        self.assertEqual(result["units"][2]["locator"], "table 1 row 1")
        self.assertEqual(result["units"][3]["locator"], "paragraph 5")
        self.assertTrue(all(u["section"] == "Methods" for u in result["units"]))

    def test_changes_comments_notes_and_media(self):
        path = self.docx('''<w:p><w:commentRangeStart w:id="7"/><w:r><w:t>We propose </w:t></w:r>
        <w:del><w:r><w:delText>20</w:delText></w:r></w:del><w:ins><w:r><w:t>30</w:t></w:r></w:ins>
        <w:r><w:t> participants.</w:t><w:footnoteReference w:id="1"/></w:r><w:commentRangeEnd w:id="7"/></w:p>''', **{
            "word/comments.xml": f'<w:comments xmlns:w="{W}"><w:comment w:id="7" w:author="Reviewer"><w:p><w:r><w:t>Explain power.</w:t></w:r></w:p></w:comment></w:comments>',
            "word/footnotes.xml": f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="0" w:type="separator"/><w:footnote w:id="1"><w:p><w:r><w:t>Source reference.</w:t></w:r></w:p></w:footnote></w:footnotes>',
            "word/media/image1.png": b"test-image-not-decoded",
        })
        result = extract_file(path)
        self.assertIn("30 participants.", result["units"][0]["text"])
        self.assertNotIn("20", result["units"][0]["text"])
        self.assertIn("[footnote 1]", result["units"][0]["text"])
        comment = next(u for u in result["units"] if u["kind"] == "comment")
        self.assertEqual(comment["anchors"], ["paragraph 1"])
        self.assertEqual(comment["author"], "Reviewer")
        self.assertEqual(result["metadata"]["tracked_changes"]["del"], 1)
        self.assertEqual(result["metadata"]["embedded_media"][0]["name"], "word/media/image1.png")
        self.assertTrue(any("Tracked changes" in w for w in result["warnings"]))
        self.assertEqual(sum(u["kind"] == "footnote" for u in result["units"]), 1)

    def test_doctype_rejected(self):
        path = self.root / "entity.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", '<!DOCTYPE foo [<!ENTITY x "bad">]><foo>&x;</foo>')
        result = extract_file(path)
        self.assertEqual(result["metadata"]["status"], "error")
        self.assertTrue(any("DTD" in w for w in result["warnings"]))

    def test_pdf_physical_page_locators_and_sparse_warning(self):
        output = "A meaningful first paragraph with sufficient words for text extraction.\n\nSecond block.\fFigure 1\f"
        completed = subprocess.CompletedProcess([], 0, output, "")
        with patch("grantlib.extract.shutil.which", return_value="/tools/pdftotext"), patch("grantlib.extract._command", return_value=completed):
            result = extract_file(self.root / "source.pdf")
        self.assertEqual(result["metadata"]["page_count"], 2)
        self.assertEqual(result["units"][1]["locator"], "page 1, block 2")
        self.assertEqual(result["units"][2]["page"], 2)
        self.assertEqual(result["metadata"]["low_text_pages"], [2])

    def test_xfa_placeholder_is_not_substantive_content(self):
        stub = ("Please wait...\nIf this message is not eventually replaced by the proper contents of the document, your PDF\n"
                "viewer may not be able to display this type of document.\n\n"
                "You can upgrade to the latest version of Adobe Reader for Windows, Mac, or Linux by\n"
                "visiting http://www.adobe.com/go/reader_download.\n\n"
                "For more assistance with Adobe Reader visit http://www.adobe.com/go/acrreader.\n"
                "Windows is either a registered trademark or a trademark of Microsoft Corporation.\f")
        completed = subprocess.CompletedProcess([], 0, stub, "")
        with patch("grantlib.extract.shutil.which", return_value="pdftotext"), patch("grantlib.extract._command", return_value=completed):
            result = extract_file(self.root / "xfa.pdf")
        self.assertEqual(result["units"], [])
        self.assertEqual(result["metadata"]["status"], "no_text")
        self.assertEqual(result["metadata"]["xfa_placeholder_pages"], [1])
        self.assertEqual(len(result["metadata"]["omitted_viewer_placeholder_blocks"]), 3)
        self.assertTrue(any("Adobe/XFA" in warning for warning in result["warnings"]))

    def test_xfa_stub_retains_other_page_content_and_original_locators(self):
        output = ("Please wait...\n\nIf this message is not eventually replaced by the proper contents of the document, "
                  "your PDF viewer may not be able to display this type of document.\n\n"
                  "Study aim: measure root damage after irrigation using histological endpoints.\f"
                  "Please wait until the experiment reaches the target temperature before taking measurements.\f")
        completed = subprocess.CompletedProcess([], 0, output, "")
        with patch("grantlib.extract.shutil.which", return_value="pdftotext"), patch("grantlib.extract._command", return_value=completed):
            result = extract_file(self.root / "mixed.pdf")
        self.assertEqual(result["metadata"]["status"], "partial")
        self.assertEqual(len(result["units"]), 2)
        self.assertEqual(result["units"][0]["locator"], "page 1, block 3")
        self.assertEqual(result["units"][1]["page"], 2)
        self.assertIn("target temperature", result["units"][1]["text"])

    def test_repeated_scientific_headings_are_preserved(self):
        output = "APPROACH\n\nWe will measure auxin signaling in this first experiment.\fAPPROACH\n\nWe will measure nitrate production in the second experiment.\f"
        completed = subprocess.CompletedProcess([], 0, output, "")
        with patch("grantlib.extract.shutil.which", return_value="pdftotext"), patch("grantlib.extract._command", return_value=completed):
            result = extract_file(self.root / "headings.pdf")
        self.assertEqual(sum(u["text"] == "APPROACH" for u in result["units"]), 2)

    def test_tool_errors_are_explicit(self):
        with patch("grantlib.extract.shutil.which", return_value=None):
            result = extract_file(self.root / "source.pdf")
        self.assertEqual(result["metadata"]["status"], "error")
        self.assertIn("not installed", " ".join(result["warnings"]))
        with patch("grantlib.extract.shutil.which", return_value="pdftotext"), patch("grantlib.extract._command", side_effect=subprocess.TimeoutExpired("pdftotext", 90)):
            result = extract_file(self.root / "source.pdf")
        self.assertEqual(result["metadata"]["status"], "error")
        self.assertIn("TimeoutExpired", " ".join(result["warnings"]))

    def test_unsupported_inventory_only_and_utf8_errors(self):
        result = extract_file(self.root / "figure.ai")
        self.assertEqual(result["metadata"]["status"], "unsupported")
        path = self.root / "bad.txt"
        path.write_bytes(b"\xff\xfe")
        result = extract_file(path)
        self.assertEqual(result["metadata"]["status"], "error")

    @unittest.skipUnless(shutil.which("textutil"), "RTF conversion requires macOS textutil")
    def test_rtf_extracts_text_without_modifying_original(self):
        path = self.root / "proposal.rtf"
        original = br"{\rtf1\ansi We will measure chlorophyll in leaf tissue.\par An independent aim.}"
        path.write_bytes(original)
        result = extract_file(path)
        text = "\n".join(unit["text"] for unit in result["units"])
        self.assertIn("We will measure chlorophyll in leaf tissue.", text)
        self.assertIn("An independent aim.", text)
        self.assertNotIn("\\rtf", text)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(any("RTF converted" in w for w in result["warnings"]))

    def test_deleted_paragraph_not_emitted(self):
        path = self.docx('<w:del><w:p><w:r><w:t>Obsolete</w:t></w:r></w:p></w:del><w:p><w:r><w:t>Current</w:t></w:r></w:p>')
        result = extract_file(path)
        self.assertEqual([u["text"] for u in result["units"]], ["Current"])
        self.assertEqual(result["units"][0]["locator"], "paragraph 2")

    def test_alternate_views_not_duplicated_and_math_preserved(self):
        path = self.docx('''<w:p xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
        <mc:AlternateContent><mc:Choice><w:r><w:t>Figure title</w:t></w:r></mc:Choice><mc:Fallback><w:r><w:t>Figure title</w:t></w:r></mc:Fallback></mc:AlternateContent>
        <m:oMath><m:r><m:t>x=2</m:t></m:r></m:oMath></w:p>''')
        result = extract_file(path)
        text = result["units"][0]["text"]
        self.assertEqual(text.count("Figure title"), 1)
        self.assertIn("x=2", text)
        self.assertTrue(any("Equation text" in w for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
