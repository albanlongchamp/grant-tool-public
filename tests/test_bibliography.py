"""Deterministic citation formatting over invented metadata; no network or model."""
import json
import unittest

from grantlib import bibliography as bib


def metadata(**updates):
    base = {'title': 'A fictional paired study of nothing', 'journal': 'Invented Journal of Testing',
            'journal_abbreviation': 'Invent J Test', 'year': 2024, 'volume': '12', 'issue': '3', 'pages': '100-110',
            'canonical': {'pmid': '12345678', 'doi': '10.9999/fictional'},
            'authors': ['Example Author', 'Second Writer', 'Third Person'],
            'author_parts': [{'family': 'Author', 'given': 'Example'}, {'family': 'Writer', 'given': 'Second Jean-Pierre'},
                             {'family': 'Person', 'given': 'Third'}]}
    base.update(updates)
    return base


def source(source_id, layer='publication', **updates):
    data = metadata(**updates)
    return {'id': source_id, 'layer': layer, 'title': data['title'], 'source_uri': 'https://example.org/' + source_id,
            'metadata': data}


class CslTests(unittest.TestCase):
    def test_csl_item_uses_structured_authors_and_identifiers(self):
        item = bib.csl_item(metadata(), 'R_1')
        self.assertEqual(item['type'], 'article-journal')
        self.assertEqual(item['author'][1], {'family': 'Writer', 'given': 'Second Jean-Pierre'})
        self.assertEqual(item['issued'], {'date-parts': [[2024]]})
        self.assertEqual((item['DOI'], item['PMID'], item['page']), ('10.9999/fictional', '12345678', '100-110'))
        self.assertNotIn('abstract', item)
        json.loads(bib.csl_json([item]))

    def test_plain_author_strings_are_parsed_conservatively(self):
        self.assertEqual(bib.parse_name('Example Author'), {'family': 'Author', 'given': 'Example'})
        self.assertEqual(bib.parse_name('Anna van der Berg'), {'family': 'van der Berg', 'given': 'Anna'})
        self.assertEqual(bib.parse_name('Berg, Anna'), {'family': 'Berg', 'given': 'Anna'})
        self.assertEqual(bib.parse_name('Invented Study Group'), {'literal': 'Invented Study Group'})
        self.assertEqual(bib.parse_name('Plato'), {'literal': 'Plato'})
        self.assertIsNone(bib.parse_name('  '))
        item = bib.csl_item(metadata(author_parts=[]), 'R_2')
        self.assertEqual(item['author'][0], {'family': 'Author', 'given': 'Example'})

    def test_every_style_renders_each_field_once(self):
        item = bib.csl_item(metadata(), 'R_1')
        expected = {
            'vancouver': 'Author E, Writer SJP, Person T. A fictional paired study of nothing. Invent J Test. 2024;12(3):100-110. doi:10.9999/fictional. PMID: 12345678.',
            'nature': 'Author, E., Writer, S. J.-P. & Person, T. A fictional paired study of nothing. *Invent J Test* **12**, 100–110 (2024).',
            'science': 'E. Author, S. J.-P. Writer, T. Person, A fictional paired study of nothing. *Invent J Test* **12**, 100–110 (2024).',
            'cell': 'Author, E., Writer, S.J.-P., and Person, T. (2024). A fictional paired study of nothing. *Invent J Test* *12*, 100–110. https://doi.org/10.9999/fictional.',
            'nejm': 'Author E, Writer SJP, Person T. A fictional paired study of nothing. Invent J Test 2024;12:100-110.',
            'jama': 'Author E, Writer SJP, Person T. A fictional paired study of nothing. *Invent J Test*. 2024;12(3):100-110. doi:10.9999/fictional',
            'apa': 'Author, E., Writer, S. J.-P., & Person, T. (2024). A fictional paired study of nothing. *Invented Journal of Testing*, *12*(3), 100–110. https://doi.org/10.9999/fictional',
        }
        self.assertEqual(set(expected), set(bib.style_names()))
        for style, text in expected.items():
            with self.subTest(style=style):
                self.assertEqual(bib.format_entry(item, style), text)

    def test_author_truncation_follows_each_style(self):
        many = [{'family': f'Family{i}', 'given': 'Given'} for i in range(1, 9)]
        item = bib.csl_item(metadata(author_parts=many), 'R_1')
        self.assertTrue(bib.format_entry(item, 'vancouver').startswith('Family1 G, Family2 G, Family3 G, Family4 G, Family5 G, Family6 G, et al. '))
        self.assertTrue(bib.format_entry(item, 'nejm').startswith('Family1 G, Family2 G, Family3 G, et al. '))
        self.assertTrue(bib.format_entry(item, 'nature').startswith('Family1, G. et al. A fictional'))
        self.assertTrue(bib.format_entry(item, 'science').startswith('G. Family1 et al., A fictional'))
        self.assertIn('Family8, G. (2024)', bib.format_entry(item, 'cell'))
        collective = bib.csl_item({'title': 'Consensus statement', 'authors': ['Invented Study Group'], 'journal': 'J Fict', 'year': 2020}, 'R_3')
        self.assertEqual(bib.format_entry(collective, 'nature'), 'Invented Study Group. Consensus statement. *J Fict* (2020).')
        self.assertEqual(bib.format_entry(bib.csl_item({'title': 'Orphan record'}, 'R_4'), 'vancouver'), 'Orphan record. n.d.')

    def test_unknown_style_is_rejected_with_the_list(self):
        with self.assertRaises(ValueError) as caught:
            bib.check_style('chicago')
        self.assertIn('vancouver', str(caught.exception))
        self.assertEqual([s['style'] for s in bib.describe_styles()], bib.style_names())


class DraftSubstitutionTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            'R_1': source('R_1'),
            'R_2': source('R_2', title='Other', authors=['Example Author', 'Second Writer'],
                          author_parts=[{'family': 'Author', 'given': 'Example'}, {'family': 'Writer', 'given': 'Second'}]),
            'R_3': source('R_3', title='Same label', authors=['Example Author'], author_parts=[{'family': 'Author', 'given': 'Example'}], year=2024),
            'R_4': source('R_4', title='Same label again', authors=['Example Author'], author_parts=[{'family': 'Author', 'given': 'Example'}], year=2024),
            'P_x': {'id': 'P_x', 'layer': 'archive', 'title': 'Old aims page', 'source_uri': '', 'metadata': {'locator': 'p. 3'}},
        }
        self.text = 'One [[R_1]]. Two [[R_2]], [[R_1]] and local [[P_x]][[R_2]]. Twins [[R_4]] then [[R_3]]. Missing [[R_9]].'

    def test_numeric_styles_keep_appearance_numbering_and_list_local_sources(self):
        cited = bib.Bibliography(self.text, self.sources, [], 'vancouver')
        self.assertEqual(cited.substitute(self.text),
                         'One [1]. Two [2], [1] and local [3][2]. Twins [4] then [5]. Missing [6].')
        rows = cited.entries()
        self.assertEqual([n for n, _i, _t in rows], [1, 2, 3, 4, 5, 6])
        self.assertIn('local source P_x', rows[2][2])
        self.assertIn('Unresolved source R_9', rows[5][2])
        markdown = cited.markdown()
        self.assertTrue(markdown.startswith('## References\n'))
        self.assertIn('\n1. Author E, Writer SJP, Person T.', markdown)
        self.assertIn('not been independently verified', markdown)
        self.assertEqual([i['id'] for i in cited.csl_items()], ['R_1', 'R_2', 'R_4', 'R_3'])

    def test_author_year_styles_merge_adjacent_markers_and_disambiguate(self):
        cited = bib.Bibliography(self.text, self.sources, [], 'cell')
        rendered = cited.substitute(self.text)
        self.assertIn('One (Author et al., 2024).', rendered)
        self.assertIn('Two (Author and Writer, 2024; Author et al., 2024) and local (Author and Writer, 2024)[3].', rendered)
        self.assertIn('Twins (Author, 2024b) then (Author, 2024a).', rendered)
        self.assertIn('Missing [6].', rendered)
        markdown = cited.markdown()
        self.assertIn('Author, E. (2024a). Same label.', markdown)
        self.assertIn('Author, E. (2024b). Same label again.', markdown)
        self.assertLess(markdown.index('(2024a)'), markdown.index('(2024b)'))
        self.assertIn('\n\n[3] Old aims page — p. 3', markdown)
        self.assertEqual(bib.in_text_preview(cited.items['R_2'], 'apa'), '(Author and Writer, 2024)')
        self.assertIn('superscript', bib.in_text_preview(cited.items['R_2'], 'nature'))

    def test_superscript_styles_attach_and_compress_adjacent_numbers(self):
        cited = bib.Bibliography(self.text, self.sources, [], 'nature')
        self.assertEqual(cited.substitute(self.text), 'One[1]. Two[1,2] and local[2,3]. Twins[4] then[5]. Missing[6].')
        self.assertEqual(bib._ranges([1, 2, 3, 5, 7, 8, 10]), '1–3,5,7,8,10')
        self.assertEqual(bib.Bibliography('A [[R_1]] [[R_2]] [[P_x]] [[R_3]]', self.sources, [], 'jama').substitute('A [[R_1]] [[R_2]] [[P_x]] [[R_3]]'), 'A[1–4]')

    def test_full_text_sources_borrow_volume_and_authors_from_the_retained_reference(self):
        thin = {'id': 'F_1', 'layer': 'publication', 'title': 'Entirely invented public article', 'source_uri': 'https://example.org/f',
                'metadata': {'canonical': {'pmcid': 'PMC1', 'pmid': '555'}, 'authors': ['Example Author'], 'locator': 'Methods, paragraph 1'}}
        reference = {'id': 'R_7', 'canonical': {'pmid': '555', 'doi': '10.9999/f'}, 'title': 'Entirely invented public article',
                     'author_parts': [{'family': 'Author', 'given': 'Example'}, {'family': 'Writer', 'given': 'Second'}],
                     'journal_abbreviation': 'Invent J', 'year': 2023, 'volume': '2', 'pages': '1-9'}
        cited = bib.Bibliography('Claim [[F_1]].', {'F_1': thin}, [reference], 'vancouver')
        self.assertEqual(cited.entries()[0][2], 'Author E, Writer S. Entirely invented public article. Invent J. 2023;2:1-9. PMID: 555.')
        self.assertEqual(cited.markdown(), '') if not cited.used else None
        self.assertEqual(bib.Bibliography('No citations.', {}, [], 'nature').markdown(), '')


class RisTests(unittest.TestCase):
    def test_ris_records_are_importable_fields(self):
        item = bib.csl_item(metadata(abstract='Invented  abstract\nwith breaks.'), 'R_1')
        text = bib.ris([item, bib.csl_item({'title': 'Book chapter', 'work_type': 'book-chapter', 'authors': ['Invented Study Group']}, 'R_2')])
        records = text.strip().split('\nER  - ')
        self.assertEqual(len(records), 2)
        first = records[0].splitlines()
        self.assertEqual(first[0], 'TY  - JOUR')
        self.assertIn('AU  - Writer, Second Jean-Pierre', first)
        self.assertIn('SP  - 100', first)
        self.assertIn('EP  - 110', first)
        self.assertIn('DO  - 10.9999/fictional', first)
        self.assertIn('AN  - 12345678', first)
        self.assertIn('AB  - Invented abstract with breaks.', first)
        self.assertIn('TY  - BOOK', records[1])
        self.assertIn('AU  - Invented Study Group', records[1])
        self.assertTrue(text.endswith('ER  - \n'))


if __name__ == '__main__':
    unittest.main()
