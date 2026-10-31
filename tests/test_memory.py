from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from grantlib import memory
from grantlib.studio import Conflict, Store


def entry(**updates):
    value = {'category': 'capability', 'text': 'The fictional core can process coded images.',
             'scope': 'Only the invented cell-culture workflow.',
             'provenance_note': 'Invented investigator statement for an offline test.',
             'tags': ['imaging'], 'review_after': ''}
    value.update(updates)
    return value


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        with self.store.connect() as c:
            memory.initialize(c)
        self.p = self.store.create({'title': 'Invented proposal', 'question': 'What can this fictional experiment establish?'})

    def tearDown(self):
        self.temp.cleanup()

    def catalog(self):
        with self.store.connect() as c:
            return memory.catalog(c)

    def create(self, data=None, actor='investigator'):
        catalog = memory.create(self.store, self.catalog()['revision'],
                                data if data is not None else entry(status='confirmed'), actor)
        return catalog['entries'][0]

    def revise(self, item, data=None, actor='investigator'):
        catalog = memory.revise(self.store, item['id'], self.catalog()['revision'],
                                data if data is not None else entry(status='confirmed'), actor)
        return next(x for x in catalog['entries'] if x['id'] == item['id'])

    def select(self, items, note='For the invented proposal only.'):
        self.p = self.store.get(self.p['id'])
        self.p = memory.select(self.store, self.p['id'], self.p['revision'], self.catalog()['revision'],
                               [{'entry_id': x['id'], 'version_id': x['version_id'], 'note': note} for x in items])
        return self.p

    def frozen(self):
        with self.store.connect() as c:
            return memory.snapshot(c, self.p['id'])

    def bindings(self):
        with self.store.connect() as c:
            return memory.bindings(c, self.p['id'])

    def test_catalog_initialization_is_idempotent_and_default_is_proposed(self):
        self.assertEqual(self.catalog()['revision'], 1)
        item = memory.create(self.store, 1, entry())['entries'][0]
        self.assertEqual(item['status'], 'proposed')
        self.assertEqual(item['actor'], 'assistant')
        self.assertIsNone(item['confirmed_at'])
        self.assertEqual(item['version'], 1)
        self.assertEqual(len(item['history']), 1)
        with self.store.connect() as c:
            memory.initialize(c)
        self.assertEqual(self.catalog()['revision'], 2)
        self.assertEqual(self.frozen(), [])
        with self.assertRaisesRegex(Conflict, 'proposed'):
            self.select([item])

    def test_assistant_cannot_confirm_reconfirm_or_retire(self):
        for status in ('confirmed', 'retired'):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'explicit investigator'):
                self.create(entry(status=status), actor='assistant')
        confirmed = self.create()
        for status in ('confirmed', 'retired'):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'explicit investigator'):
                self.revise(confirmed, entry(status=status), actor='assistant')
        self.assertEqual(self.catalog()['entries'][0]['version'], 1)

    def test_editing_confirmed_record_creates_proposal_without_inheriting_confirmation(self):
        confirmed = self.create()
        original_version = deepcopy(confirmed['history'][0])
        self.select([confirmed])
        edited = self.revise(confirmed, entry(text='A changed fictional capability.'), actor='assistant')
        self.assertEqual(edited['status'], 'proposed')
        self.assertIsNone(edited['confirmed_at'])
        self.assertEqual(edited['version'], 2)
        self.assertEqual(edited['history'][1], original_version)
        self.assertEqual(self.bindings()[0]['current_state'], 'proposed')
        with self.assertRaisesRegex(Conflict, 'confirmation'):
            self.frozen()

    def test_investigator_edit_also_needs_explicit_confirmation_status(self):
        item = self.create()
        edited = self.revise(item, entry(), actor='investigator')
        self.assertEqual(edited['status'], 'proposed')
        self.assertIsNone(edited['confirmed_at'])
        confirmed = self.revise(edited)
        self.assertEqual(confirmed['status'], 'confirmed')
        self.assertEqual(confirmed['confirmed_at'], confirmed['created_at'])
        self.assertNotEqual(confirmed['version_id'], item['version_id'])

    def test_opt_in_selection_is_scoped_to_one_proposal_and_exact_version(self):
        item = self.create()
        other = self.store.create({'title': 'Other invented proposal', 'question': 'Another invented question'})
        self.assertEqual(self.frozen(), [])
        selected = self.select([item])
        self.assertEqual(selected['revision'], 2)
        frozen = self.frozen()[0]
        self.assertEqual(frozen['entry_id'], item['id'])
        self.assertEqual(frozen['version_id'], item['version_id'])
        self.assertEqual(frozen['note'], 'For the invented proposal only.')
        self.assertEqual(self.bindings()[0]['current_state'], 'current')
        with self.store.connect() as c:
            self.assertEqual(memory.snapshot(c, other['id']), [])

    def test_global_catalog_conflicts_protect_form_submissions(self):
        revision = self.catalog()['revision']
        item = self.create()
        with self.assertRaisesRegex(Conflict, 'Reload the memory catalog'):
            memory.create(self.store, revision, entry())
        with self.assertRaises(Conflict):
            memory.revise(self.store, item['id'], revision, entry())
        with self.assertRaises(Conflict):
            memory.select(self.store, self.p['id'], self.p['revision'], revision, [])

    def test_unrelated_catalog_change_does_not_invalidate_selected_or_queued_snapshots(self):
        selected = self.create()
        self.select([selected])
        queued = self.frozen()
        self.create(entry(status='proposed', category='writing_preference', text='Use plain language.'))
        self.assertEqual(self.bindings()[0]['current_state'], 'current')
        self.assertEqual(self.frozen(), queued)
        with self.store.connect() as c:
            memory.validate_snapshot(c, queued)

    def test_changed_selected_version_requires_reselection_and_rejects_queued_snapshot(self):
        item = self.create()
        self.select([item])
        queued = self.frozen()
        newer = self.revise(item, entry(status='confirmed', text='The fictional workflow has changed.'))
        self.assertEqual(self.bindings()[0]['current_state'], 'stale')
        with self.assertRaisesRegex(Conflict, 'older memory version'):
            self.frozen()
        with self.store.connect() as c, self.assertRaises(Conflict):
            memory.validate_snapshot(c, queued)
        self.select([newer])
        self.assertEqual(self.frozen()[0]['text'], newer['text'])
        self.assertEqual(queued[0]['text'], item['text'])

    def test_retired_version_cannot_enter_new_generation_and_has_actionable_state(self):
        item = self.create()
        self.select([item])
        self.revise(item, entry(status='retired'))
        self.assertEqual(self.bindings()[0]['current_state'], 'retired')
        with self.assertRaisesRegex(Conflict, 'retired'):
            self.frozen()
        with self.assertRaisesRegex(Conflict, 'retired'):
            self.select([item])

    def test_review_is_due_on_the_recorded_day_and_can_expire_while_queued(self):
        item = self.create(entry(status='confirmed', review_after='2030-01-02'))
        with patch('grantlib.memory._today', return_value=date(2030, 1, 1)):
            self.select([item])
            queued = self.frozen()
        with patch('grantlib.memory._today', return_value=date(2030, 1, 2)):
            self.assertEqual(self.bindings()[0]['current_state'], 'review_due')
            with self.assertRaisesRegex(Conflict, 'due for review'):
                self.frozen()
            with self.store.connect() as c, self.assertRaisesRegex(Conflict, 'due for review'):
                memory.validate_snapshot(c, queued)
        reviewed = self.revise(item, entry(status='confirmed', review_after='2031-01-02'))
        with patch('grantlib.memory._today', return_value=date(2030, 1, 2)):
            self.assertEqual(self.bindings()[0]['current_state'], 'stale')
            self.select([reviewed])
            self.assertEqual(self.bindings()[0]['current_state'], 'current')

    def test_memory_edits_are_allowed_during_jobs_but_selection_changes_are_not(self):
        item = self.create()
        self.select([item])
        queued = self.frozen()
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], 'aims')
        self.revise(item, entry(text='A changed unconfirmed method.'), actor='assistant')
        with self.store.connect() as c, self.assertRaisesRegex(Conflict, 'confirmation'):
            memory.validate_snapshot(c, queued)
        with self.assertRaisesRegex(Conflict, 'active task'):
            self.select([])
        self.store.terminal(job['id'], 'failed', 'Invented task ended.')

    def test_selection_clears_current_context_without_mutating_historical_artifact(self):
        p = self.store.get(self.p['id'])
        job = self.store.queue(p['id'], p['revision'], 'aims')
        output = {'title': 'Invented aims', 'markdown': 'An invented planned experiment with adequate text for a stored draft. Its methods and resources still need investigator decisions.',
                  'claims': [], 'directions': [], 'open_questions': []}
        self.store.complete(job['id'], {'sources': []}, output)
        before = self.store.get(self.p['id'])
        artifact = deepcopy(before['artifacts'][0])
        item = self.create()
        after = self.select([item])
        self.assertIsNone(after['context'])
        self.assertEqual(after['artifacts'][0], artifact)
        self.assertEqual(after['revision'], before['revision'] + 1)
        self.revise(item, entry(status='confirmed', text='A later fictional plan.'))
        self.assertEqual(self.store.get(self.p['id'])['artifacts'][0], artifact)

    def test_repeating_same_selection_is_idempotent_even_after_unrelated_catalog_edit(self):
        item = self.create()
        self.select([item])
        before, frozen = self.p['revision'], self.frozen()
        self.create(entry(status='proposed', text='An unrelated fictional record.'))
        self.select([item])
        self.assertEqual(self.p['revision'], before)
        self.assertEqual(self.frozen(), frozen)
        self.select([item], note='A revised scope rationale.')
        self.assertEqual(self.p['revision'], before + 1)

    def test_selection_bounds_duplicates_and_old_version_rejections_are_atomic(self):
        item = self.create()
        self.select([item])
        before, frozen = self.p['revision'], self.frozen()
        with self.assertRaisesRegex(ValueError, 'only once'):
            self.select([item, item])
        with self.assertRaisesRegex(ValueError, 'at most 12'):
            self.select([item] * 13)
        with self.assertRaisesRegex(ValueError, 'note'):
            self.select([item], note='x' * 2401)
        self.assertEqual(self.store.get(self.p['id'])['revision'], before)
        self.assertEqual(self.frozen(), frozen)
        self.revise(item)
        with self.assertRaisesRegex(Conflict, 'older memory version'):
            self.select([item])

    def test_snapshot_tampering_cannot_change_text_scope_or_attribution_under_same_id(self):
        item = self.create()
        self.select([item])
        original = self.frozen()
        for key, value in [('text', 'Invented replacement'), ('scope', 'All studies'),
                           ('provenance_note', 'A fabricated confirmation'), ('created_at', '1900-01-01'),
                           ('tags', ['changed'])]:
            altered = deepcopy(original)
            altered[0][key] = value
            with self.subTest(key=key), self.store.connect() as c, self.assertRaisesRegex(Conflict, 'differs'):
                memory.validate_snapshot(c, altered)

    def test_source_preserves_exact_text_scope_confirmation_and_version_identity(self):
        item = self.create()
        self.select([item])
        selected = self.frozen()[0]
        source = memory.source(selected)
        self.assertEqual(source['id'], 'M_' + item['version_id'])
        self.assertEqual(source['source_uri'], 'urn:grant-memory:' + item['id'] + ':' + item['version_id'])
        self.assertEqual(source['text'], item['text'])
        self.assertEqual(source['layer'], 'investigator_memory')
        self.assertEqual(source['metadata']['role'], 'investigator_statement')
        self.assertEqual(source['metadata']['evidence_status'], 'investigator_confirmed')
        self.assertEqual(source['metadata']['scope'], item['scope'])
        self.assertEqual(source['metadata']['provenance_note'], item['provenance_note'])
        self.assertEqual(source['metadata']['confirmed_at'], item['confirmed_at'])
        with self.assertRaisesRegex(ValueError, 'investigator-confirmed'):
            memory.source({**selected, 'status': 'proposed'})
        source['metadata']['tags'].append('changed')
        self.assertEqual(self.frozen()[0]['tags'], ['imaging'])

    def test_required_scope_provenance_limits_and_dates_are_enforced(self):
        invalid = [entry(text=''), entry(scope=''), entry(provenance_note=''),
                   entry(text='x' * 3001), entry(scope='x' * 1201), entry(provenance_note='x' * 2401),
                   entry(tags=['x'] * 13), entry(tags=['x' * 121]), entry(tags=['']),
                   entry(review_after='20300102'), entry(review_after='2030-02-30'),
                   entry(category='scientific_truth'), entry(status='approved'),
                   {**entry(), 'confirmed_at': '2030-01-01'}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.create(data)
        self.assertEqual(self.catalog()['entries'], [])

    def test_stale_proposal_revision_rejects_selection_and_empty_selection_removes_opt_in(self):
        item = self.create()
        old_revision = self.p['revision']
        self.select([item])
        with self.assertRaises(Conflict):
            memory.select(self.store, self.p['id'], old_revision, self.catalog()['revision'], [])
        self.select([])
        self.assertEqual(self.frozen(), [])
        self.assertEqual(self.bindings(), [])


if __name__ == '__main__':
    unittest.main()
