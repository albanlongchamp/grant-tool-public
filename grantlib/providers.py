"""Explicit text-only provider adapters for the existing proposal workflow.

Command configuration is trusted local executable code, not a remote tool input.
It inherits the caller's authentication environment. This module never logs that
environment or raw stderr and never retries or changes providers automatically.
Python callables must cooperate with cancellation; their code cannot be killed.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
import uuid

from . import model as backend


PROTOCOL = 'grantlib.model.v1'
MAX_PROMPT_BYTES = 1_500_000
MAX_RESPONSE_BYTES = 4_000_000
MAX_REQUEST_BYTES = 2_000_000
MAX_STDERR_BYTES = 131_072
_RESULT_KEYS = {'status', 'text', 'structured', 'provider', 'model', 'usage', 'run_id',
                'requested_provider', 'requested_model', 'elapsed_seconds',
                'thread_id', 'process_exit_code', 'error_code'}


def _label(value, *, optional=False):
    if value is None and optional:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Provider metadata requires a bounded nonempty identifier.')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON property.')
        result[key] = value
    return result


def _parse(text):
    return json.loads(text, object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON.')))


def _inputs(prompt, schema, timeout):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise ValueError('Supply nonempty bounded prompt text.')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError('Timeout must be positive and at most 3600 seconds.')
    try:
        frozen = _parse(_json(schema))
        if schema is not None:
            if not isinstance(frozen, dict):
                raise ValueError('Schema must be an object.')
            backend._check_schema(frozen)
        if len(_json(frozen).encode()) > MAX_REQUEST_BYTES - MAX_PROMPT_BYTES:
            raise ValueError('Oversized schema.')
    except (ValueError, TypeError, RecursionError, backend.ModelError):
        raise backend.ModelSchemaError('The requested output schema is invalid or exceeds its limit.') from None
    return frozen


def _usage(value):
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError('Invalid usage metadata.')
    for key, number in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', key)
                or type(number) not in (int, float) or not math.isfinite(number) or number < 0):
            raise ValueError('Invalid usage metadata.')
    return dict(value)


def _normalize(result, schema, *, requested_provider, requested_model):
    try:
        encoded = _json(result)
        if len(encoded.encode()) > MAX_RESPONSE_BYTES:
            raise ValueError('Oversized completion.')
        value = _parse(encoded)
        if (not isinstance(value, dict) or set(value) - _RESULT_KEYS
                or value.get('status') != 'completed'):
            raise ValueError('Expected a completed text-only result.')
        provider = _label(value.get('provider'))
        observed_model = _label(value.get('model'), optional=True)
        run_id = _label(value.get('run_id'), optional=True)
        usage = _usage(value.get('usage', {}))
        text, structured = value.get('text'), value.get('structured')
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise ValueError('Empty or nontext completion.')
        if structured is not None:
            if not isinstance(structured, dict):
                raise ValueError('Structured completion must be an object.')
            if text is not None and not backend._equal(_parse(text), structured):
                raise ValueError('Text and structured completion disagree.')
            if text is None:
                text = _json(structured)
        elif schema is not None:
            if text is None:
                raise ValueError('Missing structured completion.')
            structured = _parse(text)
            if not isinstance(structured, dict):
                raise ValueError('Structured completion must be an object.')
        elif text is None:
            raise ValueError('Empty completion.')
        elapsed = value.get('elapsed_seconds')
        if elapsed is not None and (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError('Invalid elapsed metadata.')
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise backend.ModelOutputError('The provider did not return a bounded, consistent, completed text-only response.') from None
    if schema is not None:
        try:
            backend._validate(structured, schema)
        except (backend.ModelError, ValueError, TypeError, RecursionError, OverflowError):
            raise backend.ModelSchemaError('The generated response does not satisfy the requested output schema.') from None
    return {'status': 'completed', 'text': text, 'structured': structured,
            'provider': provider, 'model': observed_model, 'usage': usage, 'run_id': run_id,
            'requested_provider': requested_provider, 'requested_model': requested_model,
            **({'elapsed_seconds': elapsed} if elapsed is not None else {})}


def _stopped(cancel_event, deadline):
    if cancel_event is not None and cancel_event.is_set():
        raise backend.ModelCancelled('Generation was cancelled; no response was accepted.')
    if time.monotonic() >= deadline:
        raise backend.ModelTimeout('The model request deadline expired; no response was accepted.')


def _safe_error(exc):
    # Preserve the workflow's fatal stale-snapshot signal without leaking its
    # caller-supplied message through an ancillary last-valid-draft path.
    from .studio import Conflict
    if isinstance(exc, Conflict):
        return Conflict('The proposal or selected memory changed; no response was accepted.')
    if isinstance(exc, TimeoutError):
        return backend.ModelTimeout('The model request deadline expired; no response was accepted.')
    if isinstance(exc, backend.ModelRequestSchemaError):
        return backend.ModelRequestSchemaError(rejected_keywords=exc.rejected_keywords,
                                               process_exit_code=exc.process_exit_code)
    for kind, message in (
            (backend.ModelCancelled, 'Generation was cancelled; no response was accepted.'),
            (backend.ModelTimeout, 'The model request deadline expired; no response was accepted.'),
            (backend.ModelAuthenticationError, 'The configured provider could not authenticate.'),
            (backend.ModelUnavailable, 'The configured provider is unavailable.'),
            (backend.ModelSchemaError, 'The generated response does not satisfy the requested output schema.'),
            (backend.ModelOutputError, 'The provider did not return a usable completed response.')):
        if isinstance(exc, kind):
            return kind(message)
    return backend.ModelError('The configured provider request failed; no response was accepted.')


def checked_generator(provider):
    """Strict adapter boundary; original schema and configured identity are frozen.

    No arbitrary provider errors/events reach the user. This does not sandbox a
    trusted Python callable or its code; it rejects late/cancelled returned output.
    """
    if not callable(provider):
        raise ValueError('The configured provider must be callable.')
    requested_provider = _label(getattr(provider, 'requested_provider', 'python_callable'))
    requested_model = backend._model_name(getattr(provider, 'requested_model', None))

    def generate(prompt, *, run_dir, schema=None, on_event=None, cancel_event=None, timeout=600):
        deadline = time.monotonic() + timeout if type(timeout) in (int, float) else 0
        frozen = _inputs(prompt, schema, timeout)
        _stopped(cancel_event, deadline)

        def event(value):
            if not on_event or not isinstance(value, dict) or value.get('type') not in ('started', 'generating', 'completed'):
                return
            safe = {'type': value['type']}
            try:
                if value.get('run_id') is not None:
                    safe['run_id'] = _label(value['run_id'])
                if 'usage' in value:
                    safe['usage'] = _usage(value['usage'])
            except ValueError:
                return
            on_event(safe)

        try:
            result = provider(prompt, run_dir=run_dir, schema=_parse(_json(frozen)),
                              on_event=event, cancel_event=cancel_event,
                              timeout=max(0.000001, deadline - time.monotonic()))
            _stopped(cancel_event, deadline)
            result = _normalize(result, frozen, requested_provider=requested_provider,
                                requested_model=requested_model)
            _stopped(cancel_event, deadline)
            return result
        except Exception as exc:
            _stopped(cancel_event, deadline)
            raise _safe_error(exc) from None

    generate.requested_provider = requested_provider
    generate.requested_model = requested_model
    return generate


@dataclass(frozen=True)
class CodexProvider:
    model: str | None = None

    def __post_init__(self):
        backend._model_name(self.model)

    @property
    def requested_provider(self):
        return 'codex'

    @property
    def requested_model(self):
        return self.model

    def __call__(self, prompt, *, run_dir, schema=None, on_event=None, cancel_event=None, timeout=600):
        return backend.generate(prompt, run_dir=run_dir, schema=schema, on_event=on_event,
                                cancel_event=cancel_event, timeout=timeout, model=self.model)


@dataclass(frozen=True, init=False)
class CommandProvider:
    """One trusted argv command per request, JSON pipes, no shell or retry.

    Configuration may run arbitrary local code with inherited credentials. It is
    not a sandbox or a remotely configurable tool. The command must send only a
    completed response on stdout; logs belong on stderr and are discarded here.
    """
    command: tuple
    requested_provider: str
    requested_model: str | None

    def __init__(self, command, *, provider='command', model=None):
        if (not isinstance(command, list) or not command or len(command) > 100
                or any(not isinstance(v, str) or not v or '\0' in v or len(v) > 65536 for v in command)
                or sum(len(v) for v in command) > 262144):
            raise ValueError('Configure a nonempty, bounded trusted argv list; shell strings are not supported.')
        object.__setattr__(self, 'command', tuple(command))
        object.__setattr__(self, 'requested_provider', _label(provider))
        object.__setattr__(self, 'requested_model', backend._model_name(model))

    def __call__(self, prompt, *, run_dir, schema=None, on_event=None, cancel_event=None, timeout=600):
        started = time.monotonic()
        frozen = _inputs(prompt, schema, timeout)
        deadline = started + timeout
        _stopped(cancel_event, deadline)
        root = Path(run_dir).expanduser()
        if root.is_symlink():
            raise ValueError('Provider output directories must not be symbolic links.')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = root.resolve() / ('command-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        request_id = directory.name
        payload = _json({'protocol': PROTOCOL, 'request_id': request_id, 'prompt': prompt,
                         'schema': frozen, 'timeout_seconds': timeout,
                         'requested_model': self.requested_model}).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError('The provider request exceeds the supported size.')
        metadata = {'run_id': request_id, 'requested_provider': self.requested_provider,
                    'requested_model': self.requested_model, 'provider': None, 'model': None,
                    'usage': {}, 'status': 'starting', 'error_code': None}
        process = None
        selector = selectors.DefaultSelector()
        stdout, sent, stderr_bytes = bytearray(), 0, 0
        try:
            process = subprocess.Popen(list(self.command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=directory, bufsize=0, shell=False,
                                       start_new_session=True)
            for stream, name, events in ((process.stdout, 'stdout', selectors.EVENT_READ),
                                         (process.stderr, 'stderr', selectors.EVENT_READ),
                                         (process.stdin, 'stdin', selectors.EVENT_WRITE)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events, name)
            metadata['status'] = 'running'
            if on_event:
                on_event({'type': 'started', 'run_id': request_id})
            while selector.get_map() or process.poll() is None:
                _stopped(cancel_event, deadline)
                for key, _ in selector.select(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                    stream, name = key.fileobj, key.data
                    if name == 'stdin':
                        try:
                            sent += os.write(stream.fileno(), payload[sent:sent + 65536])
                        except BrokenPipeError:
                            raise backend.ModelOutputError('The command provider closed its input before the complete request was delivered.') from None
                        if sent == len(payload):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                        elif name == 'stdout':
                            stdout.extend(chunk)
                            if len(stdout) > MAX_RESPONSE_BYTES:
                                raise backend.ModelOutputError('The command provider exceeded its response limit.')
                        else:
                            stderr_bytes += len(chunk)
                            if stderr_bytes > MAX_STDERR_BYTES:
                                raise backend.ModelOutputError('The command provider exceeded its diagnostic limit.')
            _stopped(cancel_event, deadline)
            metadata['process_exit_code'] = process.wait(timeout=2)
            if metadata['process_exit_code']:
                raise backend.ModelError('The command provider process failed; no response was accepted.')
            try:
                raw = _parse(stdout.decode('utf-8'))
            except (ValueError, UnicodeError, RecursionError):
                raise backend.ModelOutputError('The command provider did not return one valid JSON completion.') from None
            result = _normalize(raw, frozen, requested_provider=self.requested_provider,
                                requested_model=self.requested_model)
            _stopped(cancel_event, deadline)
            result['run_id'] = result['run_id'] or request_id
            result['elapsed_seconds'] = round(time.monotonic() - started, 3)
            metadata.update({key: result[key] for key in ('status', 'provider', 'model', 'usage')})
            (directory / 'completion.json').write_bytes(bytes(stdout))
            (directory / 'completion.json').chmod(0o600)
            if on_event:
                on_event({'type': 'completed', 'run_id': request_id, 'usage': result['usage']})
            return result
        except Exception as exc:
            safe = backend.ModelUnavailable('The command provider could not be started or read.') if isinstance(exc, OSError) else _safe_error(exc)
            code = getattr(safe, 'code', 'provider_error')
            metadata.update(status=code, error_code=code)
            raise safe from None
        finally:
            if process is not None:
                backend._stop(process)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                metadata.setdefault('process_exit_code', process.returncode)
            selector.close()
            metadata['elapsed_seconds'] = round(time.monotonic() - started, 3)
            (directory / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
            (directory / 'metadata.json').chmod(0o600)
