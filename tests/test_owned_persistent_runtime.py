"""An owned DSH generation survives the supervisor connection that created it."""
import os
from pathlib import Path
import tempfile
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sdk_source_integrity import source_snapshot


class WorkModel:
    """A finite external model stream; original DSH handles every tool call."""
    def __init__(self, command="printf 'bounded\\n' > delivery.txt", dangerous=False):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(body)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                tool = len(owner.requests) == 1
                delta = {'tool_calls': [{'index': 0, 'id': 'fixture-write', 'type': 'function',
                    'function': {'name': 'bash', 'arguments': json.dumps({'command': command,
                        'description': 'Run one bounded synthetic tool.', **({'sandbox_permissions': 'danger-full-access',
                            'justification': 'Synthetic rejected capability.'} if dangerous else {})})}}]} if tool else {'content': 'Awaiting acceptance.'}
                for part, reason in (({'role': 'assistant'}, None), (delta, None), ({}, 'tool_calls' if tool else 'stop')):
                    data = {'id': 'fixture-response', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture-model',
                            'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}
                    self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
                self.wfile.write(('data: ' + json.dumps({'id': 'fixture-response', 'object': 'chat.completion.chunk', 'created': 1,
                    'model': 'fixture-model', 'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}}) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        assert not self.thread.is_alive()


@pytest.mark.parametrize('max_requests', [1, 3])
def test_original_work_input_once_and_observed_budget(max_requests):
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    model = WorkModel()
    try:
        with tempfile.TemporaryDirectory(prefix='hpm-work-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            repo = root / 'repo'
            repo.mkdir()
            state = root / 'instance'
            state.mkdir(mode=0o700)
            settings = {'dsh_home': str(state / 'home'), 'workspace': str(repo), 'runtime_package_root': str(sdk),
                'instance_id': 'fixture-work', 'generation': 'fixture-generation', 'session_id': 'session-fixture-work',
                'runtime_configuration': {'model': {'provider': 'fixture-provider', 'model': 'fixture-model', 'configuration': {
                    'api': 'openai-completions', 'baseURL': model.base_url, 'apiKeyEnv': 'DSH_FIXTURE_MODEL_KEY',
                    'models': [{'id': 'fixture-model', 'contextWindow': 16384, 'maxTokens': 256, 'input': ['text']}]}},
                    'budget': {'max_wall_seconds': 15, 'max_model_requests': max_requests, 'max_reported_tokens': 1000,
                               'max_output_tokens_per_request': 256}}}
            carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15,
                                                environment={'DSH_FIXTURE_MODEL_KEY': 'synthetic-key'})
            try:
                sid = settings['session_id']
                assert carrier.request('session/create', {'request': {'sessionId': sid, 'cwd': str(repo),
                    'agentPreset': 'hermes-owned'}}, 'create')['result']['ok']
                args = {'request': {'sessionId': sid, 'requestId': 'input-fixture-once', 'mode': 'queue',
                        'content': [{'type': 'text', 'text': 'Perform the single bounded work.'}]}}
                accepted = carrier.request('session/prompt', args, 'prompt')['result']
                assert accepted['ok'] and accepted['value']['accepted']
                repeated = carrier.request('session/prompt', args, 'prompt-again')['result']
                assert repeated == accepted, 'Repeated supervision must return the saved first-input receipt.'
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    listing = carrier.request('session/list', {'_request': {}}, 'poll')['result']['value']['items']
                    item = next(row for row in listing if row['sessionId'] == sid)
                    if (repo / 'delivery.txt').exists() and not item['running']:
                        break
                    time.sleep(.05)
                assert (repo / 'delivery.txt').read_text() == 'bounded\n'
                assert item['running'] is False
                assert len(model.requests) == (1 if max_requests == 1 else 2)
                assert all(row.get('max_tokens', row.get('max_completion_tokens')) == 256 for row in model.requests)
                stream = carrier.stream('session/follow', {'request': {'address': {'kind': 'session', 'sessionId': sid}, 'maxMessages': 500}})
                try:
                    snapshot = next(stream)
                finally:
                    stream.close()
                assert snapshot['header']['id'] == sid and snapshot['header']['cwd'] == str(repo)
                receipt = json.loads((state / 'home/.hermes-first-input.json').read_text())
                assert receipt['status'] == 'accepted' and receipt['request_id'] == 'input-fixture-once'
                if max_requests == 3:
                    receipt['status'] = 'intent'
                    (state / 'home/.hermes-first-input.json').write_text(json.dumps(receipt))
                    count = len(model.requests)
                    with pytest.raises(OSError):
                        carrier.request('session/prompt', args, 'unknown-must-not-send')
                    time.sleep(.1)
                    assert len(model.requests) == count, 'Unknown first input cannot be resent or replaced.'
                if max_requests == 1:
                    # Original turnBoundary has no wire definition; require its durable cause instead.
                    ends = [r['event'] for r in snapshot['records'] if r['event']['type'] == 'turn/end']
                    assert len(ends) == 1 and ends[0]['data']['reason']['kind'] == 'aborted', ends
                    assert ends[0]['data']['reason']['reason']['reason'] == 'owned-budget-observed-limit'
                    budget = json.loads((state / 'home/.hermes-budget.json').read_text())
                    assert budget['stop_reason'] == 'owned-budget-observed-limit' and budget['requests'] == 1
            finally:
                carrier.shutdown_owned()
            assert carrier.close_outcome['kind'] == 'original_exit'
    finally:
        model.close()
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('boundary', ['outside', 'symlink', 'hardlink', 'danger', 'temporary'])
def test_original_bash_write_boundary_and_aliases(boundary):
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    outside_root = str(Path(os.environ.get('DSH_TEST_OUTSIDE_TMP_ROOT', tempfile.gettempdir())).resolve())
    if Path(outside_root).is_relative_to(Path('/tmp').resolve()):
        pytest.skip('A self-owned temporary root outside the actual DSH /tmp grant is required.')
    with tempfile.TemporaryDirectory(prefix='hpm-outside-', dir=outside_root) as external, tempfile.TemporaryDirectory(prefix='hpm-boundary-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        repo = root / 'repo'
        repo.mkdir()
        state = root / 'instance'
        state.mkdir(mode=0o700)
        sentinel = Path(external) / 'sentinel'
        sentinel.write_text('unchanged\n')
        quoted = __import__('shlex').quote(str(sentinel))
        command = f"printf 'changed\\n' > {quoted}"
        if boundary in {'symlink', 'hardlink'}:
            command = ('ln -s ' if boundary == 'symlink' else 'ln ') + quoted + " alias.txt && printf 'changed\\n' > alias.txt"
        elif boundary == 'temporary':
            command = "printf 'temporary\\n' > \"$TMPDIR/temporary.txt\""
        model = WorkModel(command, dangerous=boundary == 'danger')
        settings = {'dsh_home': str(state / 'home'), 'workspace': str(repo), 'runtime_package_root': str(sdk),
            'instance_id': 'fixture-boundary', 'generation': 'fixture-generation', 'session_id': 'session-fixture-boundary',
            'runtime_configuration': {'model': {'provider': 'fixture-provider', 'model': 'fixture-model', 'configuration': {
                'api': 'openai-completions', 'baseURL': model.base_url, 'apiKeyEnv': 'DSH_FIXTURE_MODEL_KEY',
                'models': [{'id': 'fixture-model', 'contextWindow': 16384, 'maxTokens': 256, 'input': ['text']}]}}}}
        carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15,
                                            environment={'DSH_FIXTURE_MODEL_KEY': 'synthetic-key'})
        try:
            sid = settings['session_id']
            assert carrier.request('session/create', {'request': {'sessionId': sid, 'cwd': str(repo), 'agentPreset': 'hermes-owned'}}, 'create')['result']['ok']
            assert carrier.request('session/prompt', {'request': {'sessionId': sid, 'requestId': 'input-boundary', 'mode': 'queue',
                'content': [{'type': 'text', 'text': 'Run the single finite boundary probe.'}]}}, 'prompt')['result']['value']['accepted']
            receipt_path = state / 'home/.hermes-tool-receipts.jsonl'
            deadline = time.monotonic() + 15
            while not receipt_path.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert receipt_path.exists(), 'Original tool settlement was not observed.'
            receipt = json.loads(receipt_path.read_text().splitlines()[0])
            assert receipt['generation'] == settings['generation'] and receipt['session_id'] == sid
            assert sentinel.read_text() == 'unchanged\n', {'boundary': boundary, 'receipt': receipt}
            if boundary == 'danger':
                assert receipt['is_error'] is True and 'exit_code' not in receipt
            elif boundary == 'temporary':
                assert receipt['exit_code'] == 0 and (state / 'home/tmp/temporary.txt').read_text() == 'temporary\n'
            else:
                assert receipt['exit_code'] != 0 and receipt['sandbox']['denied'] is True, receipt
        finally:
            carrier.shutdown_owned()
            model.close()
        assert carrier.close_outcome['kind'] == 'original_exit'
    assert source_snapshot(sdk) == before


def test_reconnect_uses_same_original_owned_instance_and_session():
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original DSH SDK is required.')
        pytest.skip('Original DSH SDK is absent; no persistent instance claim.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    with tempfile.TemporaryDirectory(prefix='hpm-persist-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        workspace = root / 'repo'
        workspace.mkdir()
        state = root / 'instance'
        state.mkdir(mode=0o700)
        settings = {'dsh_home': str(state / 'home'), 'workspace': str(workspace),
                    'runtime_package_root': str(sdk), 'instance_id': 'fixture-instance',
                    'generation': 'fixture-generation', 'session_id': 'session-fixture-persistent'}
        first = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15)
        try:
            created = first.request('session/create', {'request': {'sessionId': settings['session_id'],
                'cwd': str(workspace), 'agentPreset': 'hermes-owned'}}, 'create-first')
            assert created['result']['ok'] is True
            identity = dict(first.native_identity)
        finally:
            first.close()
        second = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15)
        try:
            listed = second.request('session/list', {'_request': {}}, 'read-reconnected')
            assert second.native_identity == identity, 'Connection loss cannot replace the original execution generation.'
            assert listed['result']['ok'] is True
            assert any(item['sessionId'] == settings['session_id'] for item in listed['result']['value']['items'])
        finally:
            second.shutdown_owned()
        assert second.close_outcome['kind'] == 'original_exit' and second.close_outcome['exit_code'] == 0
    assert source_snapshot(sdk) == before


def test_silent_owned_response_is_typed_unknown_without_replacing_session():
    """Stop only this original carrier; its unanswered public RPC must stay unknown."""
    import signal
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    from ghost_hermes_pm.repository_supervision import owned_call
    from ghost_hermes_pm.manager import ManagementError
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original DSH SDK is required.')
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    with tempfile.TemporaryDirectory(prefix='hpm-silent-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        repo = root / 'repo'
        repo.mkdir()
        state = root / 'instance'
        state.mkdir(mode=0o700)
        configuration = {'dsh_home': str(state / 'home'), 'workspace': str(repo),
            'runtime_package_root': str(sdk), 'instance_id': 'fixture-silent',
            'generation': 'fixture-silent-generation', 'session_id': 'fixture-silent-session'}
        carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=configuration, timeout=15)
        paused = False
        try:
            created = owned_call(carrier, 'session/create', {'sessionId': configuration['session_id'],
                'cwd': str(repo), 'agentPreset': 'hermes-owned'})
            assert created['sessionId'] == configuration['session_id']
            identity = dict(carrier.native_identity)
            os.kill(identity['pid'], signal.SIGSTOP)
            paused = True
            with pytest.raises(ManagementError) as failure:
                owned_call(carrier, 'session/list', {})
            assert failure.value.code == 'outcome_unknown'
            import queue
            assert isinstance(failure.value.__cause__, queue.Empty)
            assert carrier.native_identity == identity
            assert json.loads(carrier.identity_path.read_text()) == identity
        finally:
            if paused:
                os.kill(carrier.native_identity['pid'], signal.SIGCONT)
            carrier.shutdown_owned()
        assert carrier.close_outcome['kind'] == 'original_exit'
    assert source_snapshot(sdk) == before


@pytest.mark.parametrize('error', [OSError('Original owned socket failed.'), TimeoutError('Original owned reply timed out.'),
    __import__('psutil').NoSuchProcess(pid=987654321), __import__('psutil').AccessDenied(pid=987654321),
    ValueError('Unexpected malformed request.'), KeyError('Unexpected missing field.')])
def test_owned_response_preserves_known_failure_cause_and_does_not_retry(error):
    """A failure-only peer tests normalization; no SDK class or successful reply is replaced."""
    from types import SimpleNamespace
    from ghost_hermes_pm.repository_supervision import owned_call
    from ghost_hermes_pm.manager import ManagementError
    calls = []
    def request(*args):
        calls.append(args)
        raise error
    known = not isinstance(error, (ValueError, KeyError))
    with pytest.raises(ManagementError if known else type(error)) as failure:
        owned_call(SimpleNamespace(request=request), 'session/list', {})
    if known:
        assert failure.value.code == 'outcome_unknown'
        assert failure.value.__cause__ is error
        assert type(error).__name__ in str(failure.value)
    else:
        assert failure.value is error, 'Programming errors must retain their original type.'
    assert len(calls) == 1, 'An unknown original response is never automatically resent.'


def test_disappeared_original_owned_pid_retains_configuration_and_session():
    import signal
    import psutil
    from ghost_hermes_pm.dsh_owned_transport import PersistentOwnedTransport
    from ghost_hermes_pm.repository_supervision import owned_call
    from ghost_hermes_pm.manager import ManagementError
    configured = os.environ.get('DSH_TEST_SDK_ROOT')
    if not configured:
        if os.environ.get('DSH_REQUIRE_SDK_SMOKE') == '1':
            pytest.fail('Original DSH SDK is required.')
        pytest.skip('Original DSH SDK is absent.')
    sdk = Path(configured).resolve(strict=True)
    before = source_snapshot(sdk)
    with tempfile.TemporaryDirectory(prefix='hpm-lost-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        repo = root / 'repo'
        repo.mkdir()
        state = root / 'instance'
        state.mkdir(mode=0o700)
        settings = {'dsh_home': str(state / 'home'), 'workspace': str(repo), 'runtime_package_root': str(sdk),
            'instance_id': 'fixture-lost', 'generation': 'fixture-lost-generation', 'session_id': 'fixture-lost-session'}
        carrier = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15)
        resumed = None
        try:
            created = owned_call(carrier, 'session/create', {'sessionId': settings['session_id'],
                'cwd': str(repo), 'agentPreset': 'hermes-owned'})
            assert created['sessionId'] == settings['session_id']
            saved = {path: path.read_bytes() for path in (carrier.configuration_path, carrier.identity_path, carrier.birth_path)}
            identity = dict(carrier.native_identity)
            birth = json.loads(carrier.birth_path.read_text())
            original = psutil.Process(identity['pid'])
            assert original.create_time() == birth['created_at']
            carrier.close()
            os.kill(identity['pid'], signal.SIGKILL)
            assert original.wait(timeout=5) == -signal.SIGKILL
            resumed = PersistentOwnedTransport(instance_dir=str(state), configuration=settings, timeout=15)
            with pytest.raises(ManagementError) as failure:
                owned_call(resumed, 'session/list', {})
            assert failure.value.code == 'outcome_unknown'
            assert isinstance(failure.value.__cause__, psutil.NoSuchProcess)
            assert all(path.read_bytes() == value for path, value in saved.items())
            assert not (state / 'home/.hermes-first-input.json').exists(), 'Lost original identity cannot send a new input.'
        finally:
            if resumed:
                resumed.close()
            carrier.close()
            if carrier._process is not None and carrier._process.poll() is None:
                carrier.shutdown_owned()
            if carrier.socket_path.exists():
                assert not carrier.socket_path.is_symlink()
                carrier.socket_path.unlink()
                carrier.socket_path.parent.rmdir()
    assert source_snapshot(sdk) == before
