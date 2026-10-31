"""Exact passage lookup uses invented inputs only and never calls a model."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
import unittest
from unittest.mock import patch

from grantlib import audit_passages, draft_check
from grantlib.audit_passages import Registry
from tests import test_draft_check as dc


SAME = 'Preserve the subject’s unit; no efficacy is demonstrated (señal, año).'


def fixture():
    candidate = {'markdown': SAME + '\r\n\r\nUna medición repetida sigue siendo una subobservación.\n',
                 'change_summary': '', 'claims': [{'text': SAME}],
                 'directions': [{'id': 'D1', 'description': 'Comparar las condiciones emparejadas.'}]}
    return {'action': 'aims', 'candidate': candidate,
            'context': {'brief': {'title': 'Estudio ficticio', 'question': '¿Qué contraste?',
                                  'keywords': ['not an anchor surface'], 'empty': ''},
                        'investigator_instruction': SAME,
                        'investigator_decisions': [{'id': 'shared-id', 'text': SAME}],
                        'selected_version': {'id': 'real-parent', **deepcopy(candidate)},
                        'related_versions': [{'id': 'related-not-parent', 'markdown': 'Do not publish this as a parent anchor.'}],
                        'evidence_packet': {'sources': [{'id': 'shared-id', 'text': SAME,
                            'source_uri': 'urn:fictional:source', 'metadata': {'role': 'collaborator_or_template',
                            'evidence_status': 'proposed', 'scope': 'A fictional limited excerpt.'}}],
                            'selected_memory': []}},
            'repair_baseline': draft_check.baseline_snapshot(candidate)}


def ref(row):
    return {'passage_id': row['passage_id']}


def field_text(payload, binding):
    kind, field = binding['input_kind'], binding['input_field']
    context = payload['context']
    if kind == 'source': return context['evidence_packet']['sources'][0]['text']
    if kind == 'decision': return context['investigator_decisions'][0]['text']
    if kind == 'instruction': return context['investigator_instruction']
    if kind == 'brief': return context['brief'][field]
    version = (payload['candidate'] if kind == 'candidate' else
               context['selected_version'] if kind == 'parent' else payload['repair_baseline']['candidate'])
    return draft_check._version_text(version, field)


class AuditPassageTests(unittest.TestCase):
    def test_deterministic_exact_binding_without_mutation_or_cross_role_deduplication(self):
        payload = fixture(); original = deepcopy(payload)
        registry = Registry.build(payload, allow_directions=True)
        other = Registry.build(json.loads(json.dumps(payload, sort_keys=True)), allow_directions=True)
        self.assertEqual(registry.to_prompt_data(), other.to_prompt_data())
        self.assertEqual(payload, original)
        registry.check_payload(payload)
        rows = registry.to_prompt_data()['passages']
        self.assertEqual(len(rows), len({row['passage_id'] for row in rows}))
        same = [row for row in rows if row['text'].strip() == SAME]
        self.assertTrue({'candidate', 'parent', 'repair_baseline', 'source', 'decision', 'instruction'} <=
                        {row['kind'] for row in same})
        self.assertEqual(len(same), len({row['passage_id'] for row in same}))
        self.assertNotIn('related-not-parent', [row['id'] for row in rows])
        self.assertNotIn('keywords', [row['field'] for row in rows])
        for row in rows:
            binding = registry.binding(ref(row)); anchor = registry.resolve(ref(row))
            text = field_text(payload, binding)
            self.assertEqual(anchor['quote'], text[binding['start']:binding['end']])
            self.assertEqual(anchor, {key: row[key] for key in ('kind', 'id', 'field')} | {'quote': row['text']})
            self.assertEqual(binding['field_sha256'], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(binding['quote_sha256'], hashlib.sha256(anchor['quote'].encode()).hexdigest())
            self.assertEqual(binding['input_sha256'], registry.input_sha256)
            self.assertEqual(binding['registry_sha256'], registry.registry_sha256)
            self.assertTrue(anchor['quote'].strip())

    def test_mutable_views_and_caller_inputs_cannot_change_registry(self):
        payload = fixture(); registry = Registry.build(payload, allow_directions=True)
        original = registry.to_prompt_data(); reference = ref(original['passages'][0])
        view = registry.to_prompt_data(); view['passages'][0]['text'] = 'Injected prose'
        view['input_sha256'] = 'fake'; registry.resolve(reference)['quote'] = 'Injected prose'
        registry.binding(reference)['start'] = 999
        payload['candidate']['markdown'] = 'A changed candidate.'
        self.assertEqual(registry.to_prompt_data(), original)
        with self.assertRaises(ValueError): registry.check_payload(payload)
        with self.assertRaises(FrozenInstanceError): registry.input_sha256 = 'tampered'
        with self.assertRaises(TypeError): Registry('fake', 'fake', b'{}', b'{}')
        self.assertNotIn(SAME, repr(registry))

    def test_binding_is_an_immutable_original_locator_not_a_recursive_source_anchor(self):
        registry = Registry.build(fixture(), allow_directions=True)
        row = next(row for row in registry.to_prompt_data()['passages'] if row['kind'] == 'source')
        binding = registry.binding(ref(row))
        self.assertEqual((binding['input_kind'], binding['input_id'], binding['input_field']),
                         ('source', 'shared-id', 'text'))
        self.assertFalse({'kind', 'id', 'field'} & set(binding))
        anchor = registry.resolve(ref(row)); anchor['passage_binding'] = binding
        from grantlib.studio import _audit_source_anchors
        self.assertEqual(list(_audit_source_anchors(anchor)), [anchor])
        anchor['id'] = 'later-source-alias'
        self.assertEqual(binding['input_id'], 'shared-id')
        self.assertEqual(registry.binding(ref(row)), binding)

    def test_every_full_payload_change_invalidates_old_ids_even_if_visible_text_unchanged(self):
        original = fixture(); old = Registry.build(original, allow_directions=True)
        old_id = ref(old.to_prompt_data()['passages'][0])
        mutations = [
            lambda p: p['context']['evidence_packet']['sources'][0]['metadata'].update(role='own_grant'),
            lambda p: p['context']['evidence_packet']['sources'][0].update(source_uri='urn:fictional:another'),
            lambda p: p['context']['related_versions'][0].update(markdown='Changed background.'),
            lambda p: p.update(length_comparison={'direction': 'longer'}),
            lambda p: p['context']['brief'].update(question='A changed question?'),
            lambda p: p['candidate'].update(markdown='Unchanged claims, changed prose.'),
        ]
        for mutate in mutations:
            payload = deepcopy(original); mutate(payload)
            with self.subTest(mutation=mutate):
                with self.assertRaises(ValueError): old.check_payload(payload)
                new = Registry.build(payload, allow_directions=True)
                self.assertNotEqual(old.registry_sha256, new.registry_sha256)
                with self.assertRaises(ValueError): new.resolve(old_id)

    def test_direction_mode_exposes_only_supported_fields_and_binds_mode(self):
        payload = fixture()
        yes = Registry.build(payload, allow_directions=True)
        no = Registry.build(payload, allow_directions=False)
        self.assertEqual(yes.input_sha256, no.input_sha256)
        self.assertNotEqual(yes.registry_sha256, no.registry_sha256)
        self.assertFalse(any(row['field'].startswith('directions.') for row in no.to_prompt_data()['passages']))

    def test_source_can_never_resolve_as_investigator_authority(self):
        payload = fixture(); registry = Registry.build(payload, allow_directions=True)
        source = next(row for row in registry.to_prompt_data()['passages'] if row['kind'] == 'source')
        candidate = next(row for row in registry.to_prompt_data()['passages'] if row['kind'] == 'candidate')
        item = dc.finding(); item['after'] = [registry.resolve(ref(candidate))]
        item['authority'] = [registry.resolve(ref(source))]
        with self.assertRaisesRegex(ValueError, 'wrong kind'):
            draft_check.validate(dc.report(item), payload['context'], payload['candidate'], action='aims')
        decision = next(row for row in registry.to_prompt_data()['passages'] if row['kind'] == 'decision')
        item['authority'] = [registry.resolve(ref(decision))]
        draft_check.validate(dc.report(item), payload['context'], payload['candidate'], action='aims')

    def test_unicode_newlines_long_blocks_and_repeated_text_are_literal_slices(self):
        payload = fixture(); payload.pop('repair_baseline')
        text = ('e\u0301 naïve 🧪\r\n' * 100) + '\r\n\r\n' + ('X' * 5000) + '\n' + SAME + '\n\n' + SAME
        payload['candidate']['markdown'] = text
        registry = Registry.build(payload, allow_directions=True)
        rows = [row for row in registry.to_prompt_data()['passages'] if row['kind'] == 'candidate' and row['field'] == 'markdown']
        self.assertGreater(len(rows), 5)
        prior_end = 0
        for row in rows:
            binding = registry.binding(ref(row)); quote = registry.resolve(ref(row))['quote']
            self.assertLessEqual(len(quote), 1200)
            self.assertEqual(quote, text[binding['start']:binding['end']])
            self.assertFalse(text[prior_end:binding['start']].strip())
            prior_end = binding['end']
        self.assertFalse(text[prior_end:].strip())
        self.assertIn('e\u0301 naïve 🧪\r\n', rows[0]['text'])
        self.assertEqual(len(rows), len({row['passage_id'] for row in rows}))

    def test_fake_or_edited_reference_never_resolves(self):
        registry = Registry.build(fixture(), allow_directions=True)
        valid = ref(registry.to_prompt_data()['passages'][0])
        for value in ({'passage_id': 'p_'+'0'*64}, {'passage_id': valid['passage_id'].upper()},
                      {**valid, 'quote': 'Injected prose'}, {**valid, 'kind': 'decision'},
                      {}, {'passage_id': 1}, valid['passage_id']):
            with self.subTest(reference=value), self.assertRaises(ValueError): registry.resolve(value)

    def test_duplicates_invalid_metadata_baseline_and_unusable_fields_reject_build(self):
        mutations = [
            lambda p: p['context']['evidence_packet']['sources'].append(deepcopy(p['context']['evidence_packet']['sources'][0])),
            lambda p: p['context']['investigator_decisions'].append(deepcopy(p['context']['investigator_decisions'][0])),
            lambda p: p['context']['evidence_packet']['sources'][0].update(metadata=None),
            lambda p: p['context']['evidence_packet']['sources'][0].update(text='  '),
            lambda p: p['context']['investigator_decisions'][0].update(id=''),
            lambda p: p['candidate'].update(markdown='  '),
            lambda p: p['candidate'].update(change_summary=None),
            lambda p: p['candidate'].update(claims=[{'text': None}]),
            lambda p: p['repair_baseline'].update(candidate_sha256='wrong'),
            lambda p: p['repair_baseline']['candidate'].update(markdown='Changed after hashing.'),
        ]
        for mutate in mutations:
            payload = fixture(); mutate(payload)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                Registry.build(payload, allow_directions=True)

    def test_bounded_overload_fails_without_silent_selection_or_truncation(self):
        payload = fixture()
        for field, limit in [('MAX_PASSAGES', 1), ('MAX_REGISTRY_BYTES', 128)]:
            with self.subTest(limit=field), patch.object(audit_passages, field, limit):
                with self.assertRaisesRegex(ValueError, 'nothing was truncated'):
                    Registry.build(payload, allow_directions=True)
        payload['candidate']['markdown'] = 'invented ' * 100000
        with self.assertRaisesRegex(ValueError, 'nothing was truncated'):
            Registry.build(payload, allow_directions=True)

    def test_non_json_values_and_invalid_unicode_never_acquire_bindings(self):
        for invalid in (float('nan'), ('tuple',), {'not-a-set'}, '\ud800'):
            payload = fixture(); payload['extra'] = invalid
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                Registry.build(payload, allow_directions=True)
        payload = fixture(); payload['context']['brief'][1] = 'Not a string key.'
        with self.assertRaises(ValueError): Registry.build(payload, allow_directions=True)


if __name__ == '__main__':
    unittest.main()
