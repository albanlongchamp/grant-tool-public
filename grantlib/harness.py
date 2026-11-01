"""Public headless grant-development API over the persistent workflow engine.

The harness owns evidence, versioning and checks. A supplied frontier-model
adapter provides text generation through the same narrow protocol at every stage.
No browser, local web server or authentication probe is required to construct it.
"""
from pathlib import Path
import math
import time

from . import bibliography
from .studio import ACTIVE, Service, export_markdown
from .providers import CodexProvider, checked_generator


class GrantHarness:
    def __init__(self, db_path, *, provider=None, packet_builder=None):
        self.db = Path(db_path).expanduser().resolve()
        self.provider = provider if provider is not None else CodexProvider()
        self.service = Service(self.db, generate=checked_generator(self.provider),
                               packet_builder=packet_builder)
        self.store = self.service.store
        self._closed = False

    @classmethod
    def from_config(cls, path='config.local.json', **kwargs):
        from .library import load_config
        config = load_config(path)
        return cls(Path(config['data_dir']) / 'library.sqlite3', **kwargs)

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, *_):
        self.close()

    def _open(self):
        if self._closed:
            raise RuntimeError('This grant harness is closed. Open another instance to start work.')

    def close(self):
        if not self._closed:
            self.service.shutdown()
            self._closed = True

    def list(self):
        return self.store.list()

    def create(self, brief):
        self._open()
        return self.store.create(brief)

    def get(self, proposal_id):
        return self.store.get(proposal_id)

    def artifact(self, proposal_id, artifact_id):
        result = next((item for item in self.get(proposal_id)['artifacts']
                       if item['id'] == artifact_id), None)
        if result is None:
            raise ValueError('Version not found in this proposal.')
        return result

    @staticmethod
    def _revision(expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('Supply the positive expected_revision read from this proposal.')
        return expected_revision

    def update(self, proposal_id, brief, *, expected_revision):
        self._open()
        return self.store.update(proposal_id, self._revision(expected_revision), brief)

    def decide(self, proposal_id, text, *, expected_revision, actor='assistant'):
        self._open()
        return self.store.decision(proposal_id, self._revision(expected_revision), text, actor=actor)

    def save(self, proposal_id, markdown, *, expected_revision, title='Edited draft',
             base_artifact_id=None, author='assistant'):
        """Import a manual draft; this is not a generated/check-completed artifact."""
        self._open()
        if author not in ('assistant', 'human'):
            raise ValueError('Author must be assistant or human.')
        return self.store.save(proposal_id, self._revision(expected_revision), markdown,
                               title, base_artifact_id,
                               'assistant_import' if author == 'assistant' else 'human')

    def start(self, proposal_id, action, *, expected_revision, instruction='',
              base_artifact_id=None, selected_direction='', min_words=None, max_words=None):
        """Start one owned job; keep this process alive until it is terminal."""
        self._open()
        self._revision(expected_revision)
        if not base_artifact_id:
            if action in ('review', 'revise') or selected_direction:
                raise ValueError('Supply an explicit base_artifact_id for this branch-sensitive task.')
            if action in ('aims', 'strategy'):
                kinds = {'design'} if action == 'aims' else {'aims', 'strategy', 'revise', 'draft', 'design'}
                if any(item['kind'] in kinds for item in self.get(proposal_id)['artifacts']):
                    raise ValueError('Existing versions are available; supply an explicit base_artifact_id instead of selecting a branch implicitly.')
        return self.service.start(proposal_id=proposal_id, expected_revision=expected_revision,
                                  action=action, instruction=instruction,
                                  base_artifact_id=base_artifact_id, selected_direction=selected_direction,
                                  length_target={'min_words': min_words, 'max_words': max_words})

    def run(self, proposal_id, action, *, expected_revision, on_event=None, **arguments):
        """Run the full production pipeline, returning its terminal job record.

        Inspect status before using result_artifact_id. A completed job can still
        carry an incomplete scientific assessment in its retained artifact.
        """
        job = self.start(proposal_id, action, expected_revision=expected_revision, **arguments)
        if on_event:
            on_event({'type': 'started', 'job': job})
        return self.wait(job['id'], on_event=on_event)

    def job(self, job_id):
        # A persisted running state cannot outlive a demonstrably missing owner.
        # Recovery marks it failed; it never launches or resumes a request.
        self.store.recover()
        return self.store.job(job_id)

    def cancel(self, job_id):
        return self.service.cancel(job_id)

    def wait(self, job_id, *, timeout=None, on_event=None):
        """Observe an existing job. Observation timeout never starts another job.

        It also does not cancel the running task. Keep the owning harness alive
        to continue observing; close() requests cancellation of its own tasks.
        """
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout < 0):
            raise ValueError('Observation timeout must be a finite nonnegative number.')
        deadline = time.monotonic() + timeout if timeout is not None else None
        last_phase = None
        next_recovery = 0
        while True:
            now = time.monotonic()
            if now >= next_recovery:
                self.store.recover()
                next_recovery = now + 1
            job = self.store.job(job_id)
            phase = (job['status'], job['phase'])
            if phase != last_phase and on_event:
                on_event({'type': 'phase', 'job_id': job['id'], 'status': job['status'], 'phase': job['phase']})
            last_phase = phase
            if job['status'] not in ACTIVE:
                if on_event:
                    on_event({'type': 'result', 'job': job})
                return job
            remaining = deadline - time.monotonic() if deadline is not None else .1
            if remaining <= 0:
                raise TimeoutError(f'Observation timed out for job {job_id}; inspect this same job before taking another action.')
            time.sleep(min(.1, remaining))

    def resolve_reference(self, proposal_id, identifier, *, expected_revision):
        self._open()
        return self.service.resolve_reference(proposal_id, self._revision(expected_revision), identifier)

    def fetch_full_text(self, proposal_id, pmcid, *, expected_revision):
        self._open()
        return self.service.fetch_full_text(proposal_id, self._revision(expected_revision), pmcid)

    def full_text(self, proposal_id, document_id):
        return self.store.full_text(proposal_id, document_id)

    def retain_passage(self, proposal_id, document_id, paragraph_id, *, expected_revision, actor='assistant'):
        self._open()
        return self.store.retain_publication_passage(proposal_id, self._revision(expected_revision),
                                                     document_id, paragraph_id, actor=actor)

    def add_claim(self, proposal_id, artifact_id, quote, *, expected_revision):
        self._open()
        return self.store.add_claim(proposal_id, self._revision(expected_revision), artifact_id, quote)

    def assess_claim(self, proposal_id, artifact_id, claim_id, *, expected_revision, **judgment):
        self._open()
        return self.store.assess_claim(proposal_id, self._revision(expected_revision), artifact_id, claim_id, **judgment)

    def review_queue(self, proposal_id, artifact_id, offset=0, limit=20):
        """Read deterministic review candidates for one saved version; never infer support."""
        from .claim_review import review_queue
        return review_queue(self.get(proposal_id), artifact_id, offset=offset, limit=limit)

    def export(self, proposal_id, artifact_id, format='md', style=bibliography.DEFAULT_STYLE):
        """Export one saved version; style selects the reference list and in-text form.

        style=None keeps the plain numbered source-note export without a formatted
        reference list. ris/csl return the cited publications as RIS or CSL-JSON
        for EndNote, Zotero or Mendeley.
        """
        if style is not None:
            bibliography.check_style(style)
        if format == 'md':
            return export_markdown(self.get(proposal_id), artifact_id, style=style)
        if format == 'docx':
            from .docx_export import export_docx
            return export_docx(self.get(proposal_id), artifact_id, style=style)
        if format in ('ris', 'csl'):
            from .studio import cited_bibliography
            cited = cited_bibliography(self.get(proposal_id), artifact_id, style or bibliography.DEFAULT_STYLE)
            items = cited.csl_items()
            return bibliography.ris(items) if format == 'ris' else bibliography.csl_json(items)
        raise ValueError('Export format must be md, docx, ris or csl.')
