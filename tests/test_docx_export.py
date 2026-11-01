"""Word delivery checks use invented content and no files from the grant archive."""
import copy
from io import BytesIO
import unittest
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from grantlib.docx_export import export_docx, W, R, REL, CT
from grantlib.draft_check import baseline_snapshot


ARTIFACT_ID = 'a' * 32


def proposal():
    source = {'id': 'P_example', 'title': 'Earlier fictional source', 'layer': 'personal',
              'text': 'A fictional proposed experiment, not a completed result.',
              'source_uri': 'file:///synthetic/original%20proposal.docx',
              'metadata': {'locator': 'paragraph 12', 'role': 'ownership_uncertain', 'evidence_status': 'proposed'}}
    return {'brief': {'title': 'Fictional proposal', 'language': 'English'},
            'context': {'sources': [{**source, 'title': 'Newer source not used by this draft'}]},
            'artifacts': [{'id': ARTIFACT_ID, 'title': 'Fictional research aims', 'kind': 'aims',
                           'created_at': '2026-01-01T12:00:00+00:00',
                           'markdown': '# Fictional research aims\n\nWe will measure **chlorophyll** in paired plots [[P_example]]. A null contrast may remain inconclusive.\n\n## Aim 1\n\nCompare the planned intervention with its paired control.\n\n- Confirm the intervention.\n- Retain subject pairing.\n\n1. Report effect estimates.\n2. Describe their uncertainty.\n\n| Endpoint | Role |\n| --- | :---: |\n| Tissue chlorophyll | Primary |\n| Resistance | Secondary |',
                           'structured': {'open_questions': ['What intervention will be used?', 'Can tissue sampling be confirmed [[P_example]]?']},
                           'context': {'sources': [source]},
                           'citation_check': {'warnings': ['Ownership and support still need review.']}}]}


def unpack(data):
    with ZipFile(BytesIO(data)) as archive:
        return {name: ET.fromstring(archive.read(name)) for name in archive.namelist()}


def texts(root):
    return '\n'.join(''.join(p.itertext()) for p in root.iter('{' + W + '}p'))


class DocxExportTests(unittest.TestCase):
    def test_preserves_draft_source_snapshot_questions_and_warnings(self):
        p = proposal()
        original = copy.deepcopy(p)
        data = export_docx(p, ARTIFACT_ID)
        self.assertEqual(data[:2], b'PK')
        self.assertEqual(data, export_docx(p, ARTIFACT_ID))
        self.assertEqual(p, original)
        parts = unpack(data)
        content = texts(parts['word/document.xml'])
        for text in ['We will measure chlorophyll', 'A null contrast may remain inconclusive.',
                     'What intervention will be used?', 'Ownership and support still need review.',
                     'Earlier fictional source', 'paragraph 12', 'proposed', 'ownership_uncertain']:
            self.assertIn(text, content)
        self.assertNotIn('Newer source not used', content)
        self.assertNotIn('[[P_example]]', content)
        self.assertLess(content.index('Aim 1'), content.index('Investigator review notes'))
        self.assertLess(content.index('Unresolved investigator inputs'), content.index('Source notes for investigator review'))

    def test_citations_link_to_existing_source_bookmarks_without_file_relationships(self):
        parts = unpack(export_docx(proposal(), ARTIFACT_ID))
        doc = parts['word/document.xml']
        names = {n.attrib['{' + W + '}name'] for n in doc.iter('{' + W + '}bookmarkStart')}
        self.assertIn('Source_1', names)
        anchors = [n.attrib.get('{' + W + '}anchor') for n in doc.iter('{' + W + '}hyperlink')]
        self.assertTrue(anchors)
        self.assertTrue(all(a in names for a in anchors))
        self.assertFalse(any(r.attrib.get('TargetMode') == 'External' for r in parts['word/_rels/document.xml.rels']))
        self.assertIn('file:///synthetic/original%20proposal.docx', texts(doc))

    def test_editable_headings_lists_and_table_are_native_word_elements(self):
        parts = unpack(export_docx(proposal(), ARTIFACT_ID))
        doc = parts['word/document.xml']
        self.assertTrue(list(doc.iter('{' + W + '}tbl')))
        self.assertEqual(len(list(doc.iter('{' + W + '}tr'))), 3)
        self.assertEqual(len(list(doc.iter('{' + W + '}tblHeader'))), 1)
        self.assertEqual(len(list(doc.iter('{' + W + '}cantSplit'))), 3)
        self.assertTrue(list(doc.iter('{' + W + '}numPr')))
        self.assertFalse(list(doc.iter('{' + W + '}drawing')))
        self.assertFalse(list(doc.iter('{' + W + '}altChunk')))
        styles = {n.attrib.get('{' + W + '}val') for n in doc.iter('{' + W + '}pStyle')}
        self.assertTrue({'Title', 'Heading2', 'ListParagraph', 'TableText'} <= styles)
        self.assertTrue(list(doc.iter('{' + W + '}pageBreakBefore')))
        self.assertFalse(list(doc.iter('{' + W + '}trHeight')))
        borders = list(doc.iter('{' + W + '}tblBorders'))[0]
        self.assertEqual({n.attrib['{' + W + '}color'] for n in borders}, {'D9D9D9'})

    def test_package_relationships_and_content_types_resolve(self):
        parts = unpack(export_docx(proposal(), ARTIFACT_ID))
        for part in parts['[Content_Types].xml'].findall('{' + CT + '}Override'):
            self.assertIn(part.attrib['PartName'].lstrip('/'), parts)
        for source, prefix in [('_rels/.rels', ''), ('word/_rels/document.xml.rels', 'word/')]:
            for rel in parts[source].findall('{' + REL + '}Relationship'):
                if rel.attrib.get('TargetMode') != 'External':
                    self.assertIn(prefix + rel.attrib['Target'], parts)
        doc = parts['word/document.xml']
        size = next(doc.iter('{' + W + '}pgSz'))
        self.assertEqual(size.attrib, {'{' + W + '}w': '12240', '{' + W + '}h': '15840'})

    def test_external_links_allow_only_http_without_credentials(self):
        p = proposal()
        p['artifacts'][0]['markdown'] += '\n\n[Paper](https://example.org/paper?q=1&x=2) [Unsafe](javascript:alert(1)) [Secret](https://name:secret@example.org/)\n\n<script>no execution</script>'
        parts = unpack(export_docx(p, ARTIFACT_ID))
        links = [r.attrib['Target'] for r in parts['word/_rels/document.xml.rels'] if r.attrib.get('TargetMode') == 'External']
        self.assertEqual(links, ['https://example.org/paper?q=1&x=2'])
        content = texts(parts['word/document.xml'])
        self.assertIn('Unsafe', content)
        self.assertIn('<script>no execution</script>', content)

    def test_parentheses_in_doi_links_are_not_truncated(self):
        p = proposal()
        url = 'https://doi.org/10.0000/example(2026)'
        p['artifacts'][0]['markdown'] += '\n\n[Paper](' + url + ') and the public record (' + url + ').'
        parts = unpack(export_docx(p, ARTIFACT_ID))
        links = [r.attrib['Target'] for r in parts['word/_rels/document.xml.rels'] if r.attrib.get('TargetMode') == 'External']
        self.assertEqual(links, [url])

    def test_reference_styles_add_a_formatted_list_and_superscript_numerals(self):
        p = proposal()
        publication = {'id': 'R_fict', 'layer': 'publication', 'title': 'Fictional paired study', 'text': 'Invented abstract.',
                       'source_uri': 'https://pubmed.ncbi.nlm.nih.gov/12345678/',
                       'metadata': {'locator': 'Abstract', 'role': 'literature', 'evidence_status': 'publication_not_independently_verified',
                                    'canonical': {'pmid': '12345678', 'doi': '10.9999/fictional'}, 'authors': ['Example Author', 'Second Writer'],
                                    'author_parts': [{'family': 'Author', 'given': 'Example'}, {'family': 'Writer', 'given': 'Second'}],
                                    'journal': 'Invented Journal', 'journal_abbreviation': 'Invent J', 'year': 2024, 'volume': '12', 'issue': '3', 'pages': '100-110'}}
        p['artifacts'][0]['context']['sources'].append(publication)
        p['artifacts'][0]['markdown'] += '\n\nA published fictional result supports the design [[R_fict]], as does the earlier plan [[P_example]][[R_fict]].'
        with self.assertRaises(ValueError):
            export_docx(p, ARTIFACT_ID, style='chicago')
        for style, marker in (('vancouver', '[2]'), ('nature', None), ('cell', '(Author and Writer, 2024)')):
            with self.subTest(style=style):
                doc = unpack(export_docx(p, ARTIFACT_ID, style=style))['word/document.xml']
                content = texts(doc)
                self.assertIn('References', content)
                self.assertNotIn('[[R_fict]]', content)
                self.assertLess(content.index('References'), content.index('Investigator review notes'))
                self.assertLess(content.index('A published fictional result'), content.index('References'))
                self.assertIn('Author E, Writer S. Fictional paired study. Invent J. 2024;12(3):100-110.' if style == 'vancouver' else
                              'Author, E. & Writer, S. Fictional paired study. Invent J 12, 100–110 (2024).' if style == 'nature' else
                              'Author, E., and Writer, S. (2024). Fictional paired study. Invent J 12, 100–110.', content)
                self.assertIn('[2] Fictional paired study — Abstract.', content)
                superscripts = [r for r in doc.iter('{' + W + '}vertAlign') if r.get('{' + W + '}val') == 'superscript']
                if style == 'nature':
                    self.assertTrue(superscripts)
                    body = next(par for par in doc.iter('{' + W + '}p') if 'A published fictional result' in ''.join(par.itertext()))
                    self.assertIn('design2, as does the earlier plan1,2.', ''.join(body.itertext()))
                else:
                    self.assertFalse(superscripts)
                    self.assertIn(marker, content)
                if style == 'cell':
                    self.assertIn('earlier plan (Author and Writer, 2024)[1].', content)
        plain = texts(unpack(export_docx(p, ARTIFACT_ID))['word/document.xml'])
        self.assertNotIn('Style:', plain)

    def test_unknown_citation_remains_visible_in_notes(self):
        p = proposal()
        p['artifacts'][0]['markdown'] += '\n\nUnverified claim [[P_missing]].'
        content = texts(unpack(export_docx(p, ARTIFACT_ID))['word/document.xml'])
        self.assertIn('Unresolved source: P_missing', content)
        self.assertNotIn('[[P_missing]]', content)

    def test_recorded_judgment_keeps_exact_read_scope_and_excerpt(self):
        p = proposal()
        p['artifacts'][0]['structured']['open_questions'] = []
        p['artifacts'][0]['claims'] = [{
            'id': 'c1', 'text': 'A null result establishes absence of effect.',
            'assessments': [{'source_id': 'reference:abc', 'verdict': 'contradicted', 'scope': 'abstract',
                'reviewer': 'investigator', 'created_at': '2026-01-02T00:00:00Z',
                'note': 'The observed uncertainty does not justify that inference.',
                'excerpt': 'A non-significant contrast remained *inconclusive*.\n# Literal source heading\n[7] remained unresolved.', 'locator': 'abstract sentence 2',
                'warnings': ['Collaborator provenance requires investigator review.'],
                'source_snapshot': {'title': 'Fictional public abstract', 'source_uri': 'https://example.org/abstract',
                                    'metadata': {'role': 'collaborator', 'evidence_status': 'proposed'}}}]}]
        doc = unpack(export_docx(p, ARTIFACT_ID))['word/document.xml']
        content = texts(doc)
        self.assertIn('Recorded claim judgments', content)
        self.assertIn('Contradicted', content)
        self.assertIn('abstract sentence 2', content)
        self.assertIn('A non-significant contrast remained *inconclusive*.', content)
        self.assertIn('# Literal source heading', content)
        self.assertIn('[7] remained unresolved.', content)
        self.assertIn('Evidence labels: collaborator / proposed', content)
        self.assertIn('Review limitation: Collaborator provenance requires investigator review.', content)
        excerpt_paragraph = next(p for p in doc.iter('{' + W + '}p') if 'Read excerpt: ' in ''.join(p.itertext()))
        self.assertFalse(list(excerpt_paragraph.iter('{' + W + '}i')))
        self.assertFalse(list(excerpt_paragraph.iter('{' + W + '}hyperlink')))
        self.assertEqual(len(list(excerpt_paragraph.iter('{' + W + '}br'))), 2)
        self.assertLess(content.index('Investigator review notes'), content.index('Recorded claim judgments'))

    def test_table_escaped_pipes_and_ordered_start_are_preserved(self):
        p = proposal()
        p['artifacts'][0]['markdown'] = '# Example\n\n3. Third step\n4. Fourth step\n\n| Measure | Detail |\n| --- | --- |\n| A \\| B | `x|y` |'
        parts = unpack(export_docx(p, ARTIFACT_ID))
        content = texts(parts['word/document.xml'])
        self.assertIn('A | B', content)
        self.assertIn('x|y', content)
        self.assertEqual(next(parts['word/numbering.xml'].iter('{' + W + '}startOverride')).attrib['{' + W + '}val'], '3')

    def test_finding_introductions_stay_with_explanation_without_gluing_long_evidence(self):
        for check in ('draft_check', 'revision_check'):
            with self.subTest(check=check):
                p = proposal()
                artifact = p['artifacts'][0]
                title = 'F1: Keep subject pairing.'
                assessment = 'Assessment: major; draft_repair.'
                # Identical prose in the main draft must not gain appendix layout.
                artifact['markdown'] = '# Invented aims\n\n' + title + '\n\n' + assessment + '\n\nDraft explanation.'
                findings = []
                for number in (1, 2):
                    findings.append({'id': 'F' + str(number), 'summary': 'Keep subject pairing.',
                                     'severity': 'major', 'resolution': 'draft_repair',
                                     'explanation': 'Explanation ' + str(number) + ': ' + 'Invented detail. ' * 120,
                                     'repair': 'Retain the paired analysis.',
                                     'sources': [{'kind': 'source', 'id': 'P_example', 'field': 'text',
                                                  'quote': 'Literal source [[token]].\n' + 'Invented excerpt. ' * 120}]})
                artifact['model_metadata'] = {check: {'status': 'incomplete', 'report': {
                    'summary': 'Two invented findings.', 'state': 'issues_found',
                    'findings': findings, 'limitations': []}}}
                doc = unpack(export_docx(p, ARTIFACT_ID))['word/document.xml']
                paragraphs = list(doc.iter('{' + W + '}p'))
                paragraph_text = lambda node: ''.join(t.text or '' for t in node.iter('{' + W + '}t'))
                kept = lambda node: node.find('{' + W + '}pPr/{' + W + '}keepNext') is not None
                same_titles = [node for node in paragraphs if paragraph_text(node) == title]
                self.assertEqual(len(same_titles), 2)
                self.assertFalse(kept(same_titles[0]))
                self.assertTrue(kept(same_titles[1]))
                assessments = [node for node in paragraphs if paragraph_text(node) == assessment]
                self.assertEqual([kept(node) for node in assessments], [False, True, True])
                for finding in findings:
                    index = next(index for index, node in enumerate(paragraphs)
                                 if paragraph_text(node) == finding['id'] + ': ' + finding['summary'] and kept(node))
                    self.assertEqual(paragraph_text(paragraphs[index + 1]), assessment)
                    self.assertEqual(paragraph_text(paragraphs[index + 2]), finding['explanation'])
                    self.assertFalse(kept(paragraphs[index + 2]))
                for node in paragraphs:
                    if paragraph_text(node).startswith(('Repair or question:', 'Literal source')):
                        self.assertFalse(kept(node))
                        self.assertIsNone(node.find('{' + W + '}pPr/{' + W + '}keepLines'))

    def test_only_saved_anchor_captions_keep_with_literal_breakable_quotes(self):
        for check in ('draft_check', 'revision_check'):
            with self.subTest(check=check):
                p = proposal()
                artifact = p['artifacts'][0]
                literal = '**Exact** [[not_a_source]] <literal>\n# Heading as text\n```\n' + 'Invented long quotation. ' * 180
                def anchor(kind, identifier, field='markdown'):
                    return {'kind': kind, 'id': identifier, 'field': field, 'quote': literal}
                before = [anchor('parent', 'actual-parent')]
                if check == 'draft_check':
                    before.append(anchor('repair_baseline', 'b' * 64))
                finding = {'id': 'F1', 'summary': 'An invented control disappeared.',
                           'severity': 'major', 'resolution': 'draft_repair',
                           'explanation': 'Check the omitted control.', 'repair': 'Restore it conditionally.',
                           'before': before, 'after': [anchor('candidate', '')],
                           'authority': [anchor('instruction', '', 'text')],
                           'sources': [anchor('source', 'P_example', 'text')]}
                artifact['model_metadata'] = {check: {'status': 'completed', 'report': {
                    'summary': 'Invented comparison.', 'state': 'issues_found',
                    'findings': [finding], 'limitations': []}}}
                caption = 'Before — parent actual-parent / markdown'
                artifact['markdown'] = '# Invented draft\n\n' + caption + '\n```text\nUnrelated main example.\n```'
                # Even the same appendix caption is free when its quote differs.
                artifact['structured']['change_summary'] = caption + '\n```text\nUnrelated model note.\n```'
                original = copy.deepcopy(p)
                parts = unpack(export_docx(p, ARTIFACT_ID))
                self.assertEqual(p, original)
                paragraphs = list(parts['word/document.xml'].iter('{' + W + '}p'))
                paragraph_text = lambda node: ''.join(t.text or '' for t in node.iter('{' + W + '}t'))
                kept = lambda node: node.find('{' + W + '}pPr/{' + W + '}keepNext') is not None
                matching = [node for node in paragraphs if paragraph_text(node) == caption]
                self.assertEqual([kept(node) for node in matching], [False, False, True])
                for group in ('before', 'after', 'authority', 'sources'):
                    for item in finding[group]:
                        kind = 'initial candidate before repair' if item['kind'] == 'repair_baseline' else item['kind']
                        label = f"{group.title()} — {kind} {item['id']} / {item['field']}"
                        index = next(index for index, node in enumerate(paragraphs)
                                     if paragraph_text(node) == label and kept(node))
                        quote = paragraphs[index + 1]
                        self.assertEqual(paragraph_text(quote), literal.replace('\n', ''))
                        self.assertFalse(kept(quote))
                        self.assertIsNone(quote.find('{' + W + '}pPr/{' + W + '}keepLines'))
                        for tag in ('hyperlink', 'b', 'i'):
                            self.assertFalse(list(quote.iter('{' + W + '}' + tag)))
                code_style = next(node for node in parts['word/styles.xml'].iter('{' + W + '}style')
                                  if node.attrib.get('{' + W + '}styleId') == 'Code')
                self.assertFalse(list(code_style.iter('{' + W + '}keepNext')))
                self.assertFalse(list(code_style.iter('{' + W + '}keepLines')))

    def test_saved_baseline_field_captions_keep_only_the_start_of_literal_text(self):
        p = proposal()
        artifact = p['artifacts'][0]
        literal = '**Initial** [[P_example]] <literal>\n# Historical heading\n```\n' + 'Long baseline. ' * 180
        initial = {'title': 'Invented initial title', 'markdown': literal,
                   'change_summary': 'Initial **notes** [[P_example]].',
                   'claims': [{'text': literal, 'basis': 'proposed_experiment', 'source_ids': ['P_example']}],
                   'directions': [{'id': 'D1', 'title': 'Initial direction', 'description': literal}],
                   'open_questions': ['Initial question [[P_example]]?']}
        metadata = artifact['context']['sources'][0]['metadata']
        metadata.update(scope='Initial **scope** <unconfirmed>.', provenance_note='Invented initial basis.')
        artifact['model_metadata'] = {'draft_check': {'status': 'incomplete', 'report': None,
            'repair_baseline': baseline_snapshot(initial)}}
        artifact['markdown'] = '# Final draft\n\nInitial main text:\n```text\nUnrelated main example.\n```'
        artifact['structured']['change_summary'] = 'Initial main text:\n```text\nUnrelated model note.\n```'
        original = copy.deepcopy(p)
        doc = unpack(export_docx(p, ARTIFACT_ID))['word/document.xml']
        self.assertEqual(p, original)
        paragraphs = list(doc.iter('{' + W + '}p'))
        paragraph_text = lambda node: ''.join(t.text or '' for t in node.iter('{' + W + '}t'))
        kept = lambda node: node.find('{' + W + '}pPr/{' + W + '}keepNext') is not None
        same_labels = [node for node in paragraphs if paragraph_text(node) == 'Initial main text:']
        self.assertEqual([kept(node) for node in same_labels], [False, False, True])
        expected = [('Initial title:', initial['title']), ('Initial main text:', literal),
                    ('Initial separate change notes:', initial['change_summary']),
                    ('Initial statement 1 — proposed_experiment:', literal),
                    ('Initial direction D1:', 'Initial direction\n' + literal),
                    ('Initial unresolved inputs:', initial['open_questions'][0]),
                    ('Scope:', metadata['scope']), ('Provenance Note:', metadata['provenance_note'])]
        for caption, text in expected:
            index = next(index for index, node in enumerate(paragraphs)
                         if paragraph_text(node) == caption and kept(node))
            quote = paragraphs[index + 1]
            self.assertEqual(paragraph_text(quote), text.replace('\n', ''))
            self.assertFalse(kept(quote))
            self.assertIsNone(quote.find('{' + W + '}pPr/{' + W + '}keepLines'))
            self.assertFalse(list(quote.iter('{' + W + '}hyperlink')))

    def test_intentional_numbering_gaps_and_nested_starts_are_not_renumbered(self):
        p = proposal()
        p['artifacts'][0]['markdown'] = '# Selected steps\n\n1. First step\n3. Third step\n  4. Fourth substep\n  5. Fifth substep\n4. Fourth step'
        parts = unpack(export_docx(p, ARTIFACT_ID))
        overrides = [(n.attrib['{' + W + '}ilvl'], next(iter(n)).attrib['{' + W + '}val'])
                     for n in parts['word/numbering.xml'].iter('{' + W + '}lvlOverride')]
        self.assertIn(('0', '3'), overrides)
        self.assertIn(('1', '4'), overrides)
        definitions = parts['word/numbering.xml'].findall('{' + W + '}abstractNum')
        ids = [node.attrib['{' + W + '}val'] for node in parts['word/numbering.xml'].iter('{' + W + '}abstractNumId')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(definitions), len(ids))
        self.assertEqual(next(definitions[0].iter('{' + W + '}start')).attrib['{' + W + '}val'], '1')
        self.assertEqual(next(definitions[1].iter('{' + W + '}start')).attrib['{' + W + '}val'], '3')

    def test_unsupported_media_math_invalid_xml_and_ragged_tables_fail_explicitly(self):
        for value, message in [('![Figure](https://example.org/picture.png)', 'embed Markdown images'),
                               ('The model is $y = x^2$.', 'editable equation'),
                               ('Text\x01control', 'Word XML'),
                               ('| A | B |\n| --- | --- |\n| only one |', 'consistent number')]:
            p = proposal()
            p['artifacts'][0]['markdown'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                export_docx(p, ARTIFACT_ID)

    def test_long_fences_keep_inner_fences_and_literal_notation(self):
        p = proposal()
        p['artifacts'][0]['markdown'] = '# Literal material\n\n````text\n```\n$y = x$\n![example](https://example.org/image)\n```\n````'
        doc = unpack(export_docx(p, ARTIFACT_ID))['word/document.xml']
        content = texts(doc)
        self.assertIn('```', content)
        self.assertIn('$y = x$', content)
        self.assertIn('![example](https://example.org/image)', content)
        literal_paragraph = next(p for p in doc.iter('{' + W + '}p') if '$y = x$' in ''.join(p.itertext()))
        self.assertFalse(list(literal_paragraph.iter('{' + W + '}hyperlink')))

    def test_missing_version_is_not_silently_exported(self):
        with self.assertRaises(KeyError):
            export_docx(proposal(), 'f' * 32)

    def test_budget_currency_and_unicode_are_preserved_as_text(self):
        p = proposal()
        p['brief']['language'] = 'Spanish'
        p['artifacts'][0]['markdown'] = '# Presupuesto propuesto\n\nPrever $100,000 para los ensayos y $50,000 para el análisis. chlorophyll α y β ≥ 2.'
        parts = unpack(export_docx(p, ARTIFACT_ID))
        content = texts(parts['word/document.xml'])
        self.assertIn('$100,000', content)
        self.assertIn('$50,000', content)
        self.assertIn('chlorophyll α y β ≥ 2.', content)
        self.assertEqual(next(parts['word/styles.xml'].iter('{' + W + '}lang')).attrib['{' + W + '}val'], 'es-ES')


if __name__ == '__main__':
    unittest.main()
